"""Run the goal-tree agent: GPT proposes a goal, the decomposer builds a tree of
verifiable subtasks, Jev picks low-level actions to complete each one, and the
action builder grows the menu when the current actions cannot finish a subtask.

Usage (from the repo root, with the venv python):
    python run_tree.py                       # fresh run, new checkpoint dir
    python run_tree.py --resume ckpt_2026..  # reload goals, tree and saved primitives
    python run_tree.py --goal "Obtain 1 wooden pickaxe" --max-goals 1

Environment: OPENAI_API_KEY (required), TYPESAFE_API_KEY (Jev; the loop falls
back to plain code without it), MC_PORT (default 54321), OPENAI_MODEL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

from dotenv import load_dotenv

load_dotenv()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--resume", default=None, help="checkpoint dir to resume")
    parser.add_argument("--goal", default=None, help="run this goal instead of asking the curriculum")
    parser.add_argument("--max-goals", type=int, default=0, help="stop after this many goals (0 = run until stopped)")
    parser.add_argument("--mc-port", type=int, default=int(os.environ.get("MC_PORT", "54321")), help="Minecraft LAN port")
    parser.add_argument("--server-port", type=int, default=3000, help="mineflayer HTTP port")
    parser.add_argument("--bot-name", default="bot", help="the bot's in-game name")
    parser.add_argument("--model", default=None, help="OpenAI model for curriculum, decomposer and builder")
    parser.add_argument("--no-jev", action="store_true", help="disable TypeSafe even when a key is set")
    parser.add_argument("--poll", type=float, default=1.0, help="seconds between status polls")
    parser.add_argument("--library", default=os.path.join("skill_library", "tree"),
                        help="shared dir of learned actions; proven ones are reloaded every run ('' = per-run only)")
    parser.add_argument("--node-seconds", type=float, default=900.0, help="wall-clock limit per subtask before it counts as exhausted")
    parser.add_argument("--long-term-goal", default=None, help="direction shown to the curriculum")
    parser.add_argument("--no-review", action="store_true", help="skip the post-goal review that writes lesson files")
    parser.add_argument("--sync-builds", action="store_true", help="run builds on the poll thread (debugging)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:  # progress lines must reach a redirected log as they happen
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 2
    if args.no_jev:
        # The bot process inherits this environment, so dropping the key here
        # disables Jev on both sides (Python JevClient and Node agentLoop).
        os.environ.pop("TYPESAFE_API_KEY", None)

    from voyager import Voyager
    from voyager.tree.builder import ActionBuilder
    from voyager.tree.experience import read_decisions
    from voyager.tree.review import GoalReviewer, LessonBook
    from voyager.tree.traversal import BotDisconnected, Traversal

    resume = args.resume is not None
    ckpt_dir = args.resume or f"ckpt_{time.strftime('%Y%m%d_%H%M%S')}"
    print(f"Bot {args.bot_name}: mineflayer port {args.server_port}, checkpoint directory {ckpt_dir}")

    voyager = Voyager(
        mc_port=args.mc_port,
        server_port=args.server_port,
        bot_username=args.bot_name,
        openai_model=args.model,
        ckpt_dir=ckpt_dir,
        resume=resume,
        long_term_goal=args.long_term_goal,
        typesafe_enabled=not args.no_jev,
    )
    env, jev, llm, curriculum = voyager.env, voyager.jev, voyager.llm, voyager.curriculum_agent
    current_path = os.path.join(ckpt_dir, "tree", "current.json")

    try:
        voyager.start()
        knowledge = voyager.knowledge()
        # lessons live next to the learned actions so they carry across runs
        book = LessonBook(args.library or ckpt_dir)
        builder = ActionBuilder(env, jev, llm, ckpt_dir=ckpt_dir, library_dir=args.library or None, lessons=book)
        log_fn = (lambda kind, rec: jev.log_only(kind, rec)) if hasattr(jev, "log_only") else None
        reviewer = None if args.no_review else GoalReviewer(llm, book, log=log_fn)
        if args.library:
            n = builder.reload(proven_only=True)
            print(f"\033[35mSkill library {args.library}: {n} learned action(s) loaded\033[0m")
        elif resume:
            builder.reload()

        from voyager.tree.decomposer import Decomposer

        def verify_live(spec) -> bool:
            try:
                return bool(env.agent_verify(spec).get("ok"))
            except Exception:
                return False

        decomposer = Decomposer(knowledge, jev, llm, verify_fn=verify_live)

        goals_run = 0
        rejected: list[str] = []
        while not args.max_goals or goals_run < args.max_goals:
            tree = None
            goal = None
            # a resumed run continues the tree it was in the middle of
            if resume and goals_run == 0 and os.path.exists(current_path):
                with open(current_path, encoding="utf-8") as fh:
                    current = json.load(fh)
                goal = current.get("goal")
                tree_path = os.path.join(ckpt_dir, "tree", f"{current.get('goal_id')}.json")
                if goal and os.path.exists(tree_path):
                    from voyager.tree.schema import Tree

                    with open(tree_path, encoding="utf-8") as fh:
                        tree = Tree.from_json(json.load(fh))
                    print(f"\033[35mResuming goal '{goal}' from {tree_path}\033[0m")
            if tree is None:
                snap = None
                try:
                    snap = env.agent_status().get("snap")
                except Exception as exc:
                    print(f"status before proposing a goal failed: {exc}")
                goal = args.goal if args.goal and goals_run == 0 else curriculum.propose_goal(snap, rejected=rejected, hints=book.test_next())
                print(f"\033[35mGoal: {goal}\033[0m")
                tree = decomposer.build(goal)  # nodes already satisfied by the world come back done

            traversal = Traversal(
                tree, env, jev, builder, knowledge=knowledge, ckpt_dir=ckpt_dir,
                poll_seconds=args.poll, node_seconds_limit=args.node_seconds,
                redecompose=decomposer.redecompose,
                async_builds=not args.sync_builds,
            )
            os.makedirs(os.path.dirname(current_path), exist_ok=True)
            with open(current_path, "w", encoding="utf-8") as fh:
                json.dump({"goal": goal, "goal_id": traversal.goal_id}, fh)
            traversal.save()
            reconnects = 0
            while True:
                try:
                    result = traversal.run()
                    break
                except BotDisconnected as exc:
                    reconnects += 1
                    print(f"\033[31mBot disconnected ({exc}); reconnecting ({reconnects}/3)\033[0m")
                    if reconnects > 3:
                        raise
                    voyager.start()  # soft reset: rejoin, keep the inventory; done nodes stay done
            success = result.get("status") == "done"
            if reviewer is not None:
                # fresh-context review of this goal's raw decision records -> lesson files
                try:
                    nodes = [n.to_json() if hasattr(n, "to_json") else dict(n) for n in traversal.tree.post_order()]
                    decisions = read_decisions(traversal.decision_log, goal_id=traversal.goal_id)
                    summary = reviewer.review(goal=goal, goal_id=traversal.goal_id, success=success, nodes=nodes, decisions=decisions)
                    print(f"\033[34mReview: lessons updated for {summary.get('families') or 'no family'}; "
                          f"{summary.get('remedies_added')} remedy(ies) added\033[0m")
                except Exception as exc:
                    print(f"\033[31mpost-goal review failed: {type(exc).__name__}: {exc}\033[0m")
            curriculum.record(goal, success)
            if not success:
                rejected.append(goal)
            goals_run += 1
            try:
                os.remove(current_path)
            except OSError:
                pass
            print(f"\033[35mGoal '{goal}' {'reached' if success else 'failed'} "
                  f"({result.get('nodes_done')} subtasks done; {builder.builds} actions built this run)\033[0m")
        return 0
    except KeyboardInterrupt:
        print("stopped by user")
        return 130
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        voyager.close()


if __name__ == "__main__":
    sys.exit(main())
