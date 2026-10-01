import os
import time

from dotenv import load_dotenv

from voyager import Voyager

load_dotenv()

# You can also use mc_port instead of azure_login, but azure_login is highly recommended
azure_login = {
    "client_id": os.environ["AZURE_CLIENT_ID"],
    "redirect_url": os.environ["AZURE_REDIRECT_URL"],
    "secret_value": os.environ["AZURE_SECRET_VALUE"],
    "version": "fabric-loader-0.14.18-1.19",
}

openai_api_key = os.environ["OPENAI_API_KEY"]

# voyager = Voyager(
#     azure_login=azure_login,
#     openai_api_key=openai_api_key,
# )

# Set RESUME=True to continue the run stored in RESUME_CKPT_DIR. With
# RESUME=False every run gets its own timestamped checkpoint directory, so a
# stale QA/skill vector store from an earlier run is never reopened.
RESUME = False
RESUME_CKPT_DIR = "ckpt"
ckpt_dir = RESUME_CKPT_DIR if RESUME else f"ckpt_{time.strftime('%Y%m%d_%H%M%S')}"
print(f"Using checkpoint directory: {ckpt_dir}")

voyager = Voyager(
    mc_port=53202,
    openai_api_key=openai_api_key,
    ckpt_dir=ckpt_dir,
    resume=RESUME,
)

# start lifelong learning
voyager.learn()
