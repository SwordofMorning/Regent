##
 # @file main.py
 # @date 2026/08/07
 # 
 # @brief Main function entrance.
 #

import sys

from mk.lib.paths import base_dir, config_dir, config_path, log_dir

from src.utils import load_api_config
from src.utils import SessionManager
from src.utils import InteractiveCLI
from src.core.agent import MyAgent

# Build metadata
try:
    from mk.lib import build_info as _build_info
except ImportError:
    _build_info = None

def main():
    ver = _build_info.VERSION if _build_info else "dev"
    print(f"[>] Initializing Dandelion Project (v{ver})...")

    # 1. Load Configurations
    cfg_path = config_path()
    config = load_api_config(cfg_path)
    if not config:
        print(f"[-] FATAL: Failed to load config at {cfg_path}.")
        sys.exit(1)

    # 2. Init Session Manager
    session_mgr = SessionManager(log_dir=log_dir())

    # 3. Init Agent
    agent = MyAgent(
        config=config,
        session_manager=session_mgr,
        workspace_dir=base_dir()
    )

    print(f"[+] Agent Initialization Successful. Model: {config['MODEL_ID']}")

    # 4. Start CLI
    cli = InteractiveCLI(agent_instance=agent, session_manager=session_mgr)
    cli.run()

if __name__ == "__main__":
    main()
