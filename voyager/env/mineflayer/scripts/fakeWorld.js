// A canned mineflayer-like bot for offline tests and FAKE_WORLD=1 serving.
// World: grass plains at y=63, oak logs in a clump to the north, stone
// below, a lava pool 3 blocks east, a pond 3 blocks west, a cow north, a zombie
// far south-west. No Minecraft, no network.
const { Vec3 } = require("vec3");
const { EventEmitter } = require("events");

function makeFakeBot(version = "1.19") {
    const mcData = require("minecraft-data")(version);

    function fakeBlock(name, pos) {
        const def = mcData.blocksByName[name];
        const empty = ["air", "cave_air", "lava", "water", "fire", "grass", "tall_grass"].includes(name);
        return {
            name,
            type: def ? def.id : 0,
            position: pos.floored(),
            biome: { name: "plains", id: 1 },
            boundingBox: empty ? "empty" : "block",
        };
    }

    const logs = [];
    for (let i = 0; i < 6; i++) logs.push(new Vec3(2, 64 + (i % 3), -10 - Math.floor(i / 3)));

    const bot = {
        version,
        registry: mcData,
        entity: {
            position: new Vec3(0.5, 64, 0.5),
            yaw: 0, // facing north (mineflayer: view dir = (-sin yaw, 0, -cos yaw))
            pitch: 0,
            onGround: true,
            isInLava: false,
            isInWater: false,
            velocity: new Vec3(0, 0, 0),
        },
        health: 20,
        food: 17,
        oxygenLevel: 20,
        experience: { level: 0, points: 0 },
        time: { timeOfDay: 2000 },
        heldItem: null,
        players: {},
        _items: [
            { name: "oak_log", count: 2 },
            { name: "oak_planks", count: 4 },
            { name: "apple", count: 1 },
        ],
        inventory: {
            slots: new Array(46).fill(null),
            items() {
                return bot._items;
            },
        },
        entities: {
            1: { id: 1, name: "cow", position: new Vec3(3, 64, -14) },
            2: { id: 2, name: "zombie", position: new Vec3(-20, 64, 20) },
            3: { id: 3, name: "item", position: new Vec3(1, 64, -2), getDroppedItem: () => ({ name: "stick", count: 1 }) },
        },
        _blocksNearby: {}, // name -> Vec3 overrides set by tests
        _controls: {},
        _looks: [],
        _dug: [],
        _placedByMe: {},
        blockAt(pos) {
            const p = pos.floored();
            for (const [name, q] of Object.entries(this._blocksNearby)) {
                if (q.floored().equals(p)) return fakeBlock(name, p);
            }
            if (logs.some((l) => l.equals(p))) return fakeBlock("oak_log", p);
            if (p.x >= 3 && p.x < 9 && p.y <= 63 && p.y >= 62) return fakeBlock("lava", p);
            if (p.x <= -3 && p.x > -9 && p.y === 63) return fakeBlock("water", p);
            if (p.y >= 64) return fakeBlock("air", p);
            if (p.y === 63) return fakeBlock("grass_block", p);
            if (p.y === 62 || p.y === 61) return fakeBlock("dirt", p);
            return fakeBlock("stone", p);
        },
        findBlocks({ matching, maxDistance = 16, count = 1 }) {
            const ids = new Set(Array.isArray(matching) ? matching : [matching]);
            const here = this.entity.position;
            const out = [];
            const push = (p) => {
                if (p.distanceTo(here) <= maxDistance && !out.some((o) => o.equals(p))) out.push(p);
            };
            for (const [name, p] of Object.entries(this._blocksNearby)) {
                const def = mcData.blocksByName[name];
                if (def && ids.has(def.id)) push(p.floored());
            }
            const r = Math.min(maxDistance, 16);
            for (let x = -r; x <= r; x++) {
                for (let z = -r; z <= r; z++) {
                    for (let y = 58; y <= 67; y++) {
                        const p = new Vec3(Math.floor(here.x) + x, y, Math.floor(here.z) + z);
                        if (ids.has(this.blockAt(p).type)) push(p);
                    }
                }
            }
            out.sort((a, b) => a.distanceTo(here) - b.distanceTo(here));
            return out.slice(0, count);
        },
        findBlock({ matching, maxDistance = 16 }) {
            const ps = this.findBlocks({ matching, maxDistance, count: 1 });
            return ps.length ? this.blockAt(ps[0]) : null;
        },
        blockAtCursor(maxDistance = 6) {
            // walk the look ray in 0.5 block steps
            const dir = new Vec3(-Math.sin(this.entity.yaw) * Math.cos(this.entity.pitch), Math.sin(this.entity.pitch), -Math.cos(this.entity.yaw) * Math.cos(this.entity.pitch));
            const eye = this.entity.position.offset(0, 1.62, 0);
            for (let t = 0.5; t <= maxDistance; t += 0.5) {
                const b = this.blockAt(eye.plus(dir.scaled(t)));
                if (b && b.boundingBox === "block") return b;
            }
            return null;
        },
        entityAtCursor() {
            return null;
        },
        async look(yaw, pitch) {
            this.entity.yaw = yaw;
            this.entity.pitch = pitch;
            this._looks.push(["look", yaw, pitch]);
        },
        async lookAt(pos) {
            const eye = this.entity.position.offset(0, 1.62, 0);
            const d = pos.minus(eye);
            this.entity.yaw = Math.atan2(-d.x, -d.z);
            this.entity.pitch = Math.atan2(d.y, Math.sqrt(d.x * d.x + d.z * d.z));
            this._looks.push(["lookAt", pos]);
        },
        setControlState(name, value) {
            this._controls[name] = value;
        },
        clearControlStates() {
            this._controls = {};
        },
        canDigBlock(block) {
            return block && block.name !== "bedrock";
        },
        async dig(block) {
            this._dug.push(block.position.floored());
        },
        async equip() {},
        async openContainer(block) {
            const items = bot._containers[block.position.floored().toString()] || [];
            return { items: () => items, close() {} };
        },
        _containers: {},
        pathfinder: { setGoal() {}, async goto() {}, setMovements() {}, stop() {}, isMoving: () => false },
    };
    Object.assign(bot, EventEmitter.prototype);
    EventEmitter.call(bot);
    bot.entities[0] = bot.entity;
    return { bot, mcData, logs };
}

module.exports = { makeFakeBot };
