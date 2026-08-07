##
 # @file src/utils/cli/interactive_cli.py
 # @date 2026/08/04
 # 
 # @brief Interactive CLI for Dandelion.
 #

import os
import shlex
import tempfile
import subprocess
import builtins

##
 # @note import toolkit
 # if success, there will be Tab auto-completion
 # if not, could still use without auto-completion.
 #
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import NestedCompleter, PathCompleter
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.formatted_text import ANSI
    HAS_PTK = True
except ImportError:
    HAS_PTK = False

from .cli_printer import CLIPrinter

##
 # @brief Interactive CLI for Dandelion workspace management.
 #
class InteractiveCLI:
    ##
     # @brief Constructor.
     # 
     # @param agent_instance instance of class MyAgent.
     # @param session_manager instance of SessionManager.
     # 
    def __init__(self, agent_instance, session_manager):
        # Assignment object.
        self.agent = agent_instance
        self.session = session_manager
        # @note This will clear staged message when you exit Dandelion without commit, which have no save function.
        self.staged_message = ""
        # Init printer.
        self.cli = CLIPrinter()

        # Initialize prompt_toolkit session with in-memory history
        if HAS_PTK:
            self.prompt_session = PromptSession(history=InMemoryHistory())
        else:
            self.prompt_session = None
        # End-if
    # End-def

    ##
     # @brief Dynamically build the context-aware completer before each prompt.
     #
     # @return comp_dict or None (if not HAS_PTK).
     #
    def _build_completer(self):
        if not HAS_PTK:
            return None

        # Gather session's info, used for `checkout` and `branch`.
        sessions = self.session.list_sessions()
        session_targets = {}
        for s in sessions:
            session_targets[s['name']] = None
            session_targets[s['id']] = None
        # End-for

        # Completion Dict.
        comp_dict = {
            'branch': {
                '-a': None,
                '-d': session_targets
            },
            'checkout': {
                **session_targets,
                '-b': None
            },
            'vim': None,
            'load': PathCompleter(expanduser=True),
            'status': None,
            'commit': None,
            'clear': None,
            'help': None,
            'quit': None,
            'exit': None
        }

        return NestedCompleter.from_nested_dict(comp_dict)
    # End-def

    ##
     # @brief Help.
     #
    def _print_help(self):
        help_text = (
            f"{self.cli.C_CYAN}\n================= WORKSPACE ================={self.cli.C_RESET}\n"
            " Git-Style Session Management:\n"
            "   branch -a             : List all available sessions.\n"
            "   branch -d <name/id>   : Delete one session.\n"
            "   checkout <name/id>    : Switch to an existing session.\n"
            "   checkout -b <name>    : Create and switch to a new session.\n\n"
            " Vim-Style Editing:\n"
            "   vim                   : Open system editor (Vim/Notepad) to draft prompt.\n"
            "   load <filepath>       : Load a local file into the prompt buffer.\n\n"
            " Core Operations:\n"
            "   status                : View current session and staged buffer.\n"
            "   commit                : Send the staged buffer to LLM.\n"
            "   clear                 : Clear the staged buffer.\n"
            "   help / quit / exit    : System commands.\n"
            f"{self.cli.C_CYAN}===================================================={self.cli.C_RESET}\n"
        )
        self.cli.raw(help_text)
    # End-def

    ##
     # @brief Map session names to exact session IDs.
     #
     # @param target Session name or ID.
     # 
     # @return ID or None.
     # 
     # @retval id Session's ID.
     # @retval None No such id or name.
     #
    def _resolve_session_id(self, target):
        sessions = self.session.list_sessions()
        for s in sessions:
            if target == s['id'] or target == s['name']:
                return s['id']
        # End-for
        return None
    # End-def

    ##
     # @brief `brach` command handle. 
     # branch -a:            list all branch;
     # branch -d <name/id>:  delete selected branch.
     #
     # @param args Terminal input.
     #
    def _cmd_branch(self, args):
        # ----- 1. List all branch -----
        if not args or args[0] == '-a':
            # Get sessions.
            sessions = self.session.list_sessions()
            self.cli.success("\nAvailable Sessions (Branches):")
            # Traverse and print.
            for s in sessions:
                mark = "*" if s["id"] == self.session.current_session_id else " "
                self.cli.raw(f" {mark} {s['name']:<20} | {s['id']}")
            # End-for
            self.cli.raw("")
        # End-if

        # ----- 2. Delete branch <name/id> -----
        elif args[0] == '-d':
            # No <name/id>.
            if len(args) < 2:
                self.cli.error("Usage: branch -d <name/id>")
                return
            # End-if

            # Get Session's ID.
            target = args[1]
            session_id = self._resolve_session_id(target)

            # Session ID not found.
            if not session_id:
                self.cli.error(f"Error: Session '{target}' not found.")
                return
            # End-if

            # Ask to delete.
            ans = input(f"{self.cli.C_YELLOW}[!]{self.cli.C_RESET} Are you sure you want to delete branch '{target}'? [y/N]: ").strip().lower()
            if ans in ['y', 'yes']:
                success, msg = self.session.delete_session(session_id)
                if success:
                    self.cli.success(msg)
                else:
                    self.cli.error(msg)
            else:
                self.cli.error("Deletion aborted.")
            # End-if
        # End-elif

        # ----- 3. Others -----
        else:
            self.cli.error(f"Unknown branch argument: {args[0]}. Try 'branch -a' or 'branch -d'.")
        # End-else
    # End-def

    ##
     # @brief `checkout` command handle. 
     # checkout -b <name/id>:    create a new session branch;
     # checkout <name/id>:       switch to one existed session branch.
     #
     # @param args Terminal input.
     #
    def _cmd_checkout(self, args):
        # Error
        if not args:
            self.cli.error("Usage: checkout <name> OR checkout -b <new_name>")
            return
        # End-if

        # ----- 1. Create new session branch -----
        if args[0] == '-b':
            # Error
            if len(args) < 2:
                self.cli.error("Error: Please provide a name for the new session.")
                return
            # End-if

            # @note Here not check duplicate name;
            # Several session with different ID could have same name.

            # Assignment session name.
            new_name = args[1]
            # Generate session ID
            new_id = self.session.create_session(new_name)
            # Refresh agent's history (nothing).
            self.agent.reload_history()
            # Print success.
            self.cli.success(f"Switched to a new session branch: '{new_name}'")
            return
        # End-if

        # ----- 2. Checkout to existed session branch -----
        target = args[0]
        session_id = self._resolve_session_id(target)

        # Session not found.
        if not session_id:
            self.cli.error(f"Error: Session '{target}' not found.")
            return
        # End-if

        # Try to switch/checkout session
        if self.session.switch_session(session_id):
            self.agent.reload_history()
            self.cli.success(f"Switched to session branch: '{target}'")
        else:
            self.cli.error(f"Error: Failed to switch to '{target}'. Directory might be corrupted.")
        # End-if
    # End-def

    ##
     # @brief `vim` command handle. 
     # Open editor and write message, saved on staged buffer.
     #
    def _cmd_vim(self):
        # Set default editor: vim on Linux and notepad on Windows.
        editor = os.environ.get('EDITOR')
        if not editor:
            editor = 'vim' if os.name != 'nt' else 'notepad'
        # End-if

        # Create temporary file for drafting
        fd, tmp_path = tempfile.mkstemp(suffix=".md", prefix="dandelion_draft_", text=True)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(self.staged_message)

            # Parse the editor command with shlex to support flags
            editor_cmd = shlex.split(editor, posix=(os.name != 'nt'))
            # Normalize the executable token on Windows to remove surrounding quotes
            if os.name == 'nt' and editor_cmd:
                editor_cmd[0] = editor_cmd[0].strip('"').strip("'")

            editor_cmd.append(tmp_path)
            subprocess.call(editor_cmd)

            # Read back user input
            with open(tmp_path, 'r', encoding='utf-8') as f:
                new_content = f.read().strip()

            if new_content != self.staged_message:
                self.staged_message = new_content
                self.cli.success("Buffer successfully updated via editor.")
            else:
                self.cli.info("Buffer unchanged.")
        finally:
            os.remove(tmp_path)
    # End-def

    ##
     # @brief `load` command handle. 
     # load <filepath>: load a file (like .md) to staged message buffer.
     #
     # @param args Terminal input.
     #
    def _cmd_load(self, args):
        # Error
        if not args:
            self.cli.error("Usage: load <filepath>")
            return
        # End-if

        # Error
        filepath = args[0]
        if not os.path.exists(filepath):
            self.cli.error(f"Error: File not found -> {filepath}")
            return
        # End-if

        # Overwrite staged message buffer which is not empty.
        if self.staged_message.strip():
            ans = input(f"{self.cli.C_YELLOW}[!]{self.cli.C_RESET} Warning: The buffer is not empty. Overwrite? [y/N]: ").strip().lower()
            if ans not in ['y', 'yes']:
                self.cli.error("Load aborted.")
                return
            # End-if
        # End-if

        # Write-in
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                self.staged_message = f.read().strip()
            self.cli.success(f"Successfully loaded {os.path.getsize(filepath)} bytes into buffer.")
        except Exception as e:
            self.cli.error(f"Error loading file: {e}")
    # End-def

    ##
     # @brief `status` command handle. 
     # Show current branch and staged message buffer.
     #
    def _cmd_status(self):
        # Print branch info.
        meta = self.session.get_current_meta()
        self.cli.info(f"\nCurrent Branch : {meta.get('name', 'Unknown')}")
        self.cli.info(f"History Turns  : {len(self.agent.history)}")

        # No staged message.
        if not self.staged_message:
            self.cli.info("Staged Buffer  : (Empty)\n")
            return
        # End-if

        # Print stated message.
        self.cli.info("Staged Buffer Preview:")
        self.cli.raw("-" * 50)
        preview = self.staged_message[:300]
        self.cli.raw(preview)
        # Cut-off
        if len(self.staged_message) > 300:
            self.cli.raw("\n... [Truncated]")
        self.cli.raw("-" * 50)
        self.cli.raw(f"    (Total: {len(self.staged_message)} chars)\n")
    # End-def

    ##
    # @brief `commit` command handle. 
    # Send message to LLM.
    #
    def _cmd_commit(self):
        if not self.staged_message.strip():
            self.cli.error("Error: Buffer is empty. Draft a message using 'vim' or 'load' first.")
            return

        self.cli.raw(f"\n{self.cli.C_CYAN}================ COMMIT PREVIEW ================{self.cli.C_RESET}")
        preview = self.staged_message[:500]
        self.cli.raw(preview + ("\n... [Truncated]" if len(self.staged_message) > 500 else ""))
        self.cli.raw(f"{self.cli.C_CYAN}================================================{self.cli.C_RESET}")

        ans = input(f"{self.cli.C_CYAN}[?]{self.cli.C_RESET} Proceed to send to LLM? [y/N]: ").strip().lower()
        if ans in ['y', 'yes']:
            self.agent.inject_user_message(self.staged_message)
            self.staged_message = ""
            self.cli.info("Inference Engine Started...\n")
            while self.agent.step():
                pass
        else:
            self.cli.error("Send cancelled.")
        # End-if
    # End-def

    ##
     # @brief Run class InteractiveCLI. 
     # Send message to LLM.
     #
    def run(self):
        self.cli.raw(f"\n{self.cli.C_CYAN}================ SHELL READY ================{self.cli.C_RESET}")

        # Try to load HAS_PTK (tab completion)
        if HAS_PTK:
            self.cli.success("Bash-style Tab completion enabled (Powered by prompt_toolkit).")
        else:
            self.cli.error("prompt_toolkit not found. Fallback to basic input. (pip install prompt_toolkit)")
        # End-if

        # Print help
        self._print_help()

        # Track consecutive errors to prevent infinite loop of death
        consecutive_errors = 0

        # Interactive Loop
        while True:
            try:
                # ----- @par 1. Background task check (Agent running) -----

                if self.agent.history and self.agent.history[-1]["role"] == "user":
                    content = self.agent.history[-1]["content"]
                    if isinstance(content, list) and len(content) > 0 and content[0].get("type") == "tool_result":
                        self.cli.info("\nProcessing pending tool returns in background...")

                        initial_history_len = len(self.agent.history)

                        while self.agent.step():
                            pass

                        # Safeguard: If step() failed due to an API Error, the history size remains unchanged.
                        # We must pop the stuck tool_result to break the infinite 429 retry loop.
                        if len(self.agent.history) == initial_history_len:
                            self.cli.error("\nFATAL: Background execution blocked by an API Error.")
                            self.cli.error("Dropping the pending tool result to prevent infinite API retry loop.")
                            self.agent.history.pop()
                            self.session.save_history(self.agent.history)
                        # End-if

                        continue
                    # End-if
                # End-if

                # 2. ----- @par UI Prompt Render -----

                meta = self.session.get_current_meta()
                branch_name = meta.get("name", "unknown")
                dirty_flag = "*" if self.staged_message.strip() else ""

                # Fetch model name and format it to be clean (e.g. "nvidia/nemotron" -> "nemotron")
                full_model_name = self.agent.config.get("MODEL_ID", "dandelion")
                short_model_name = full_model_name.split("/")[-1] if "/" in full_model_name else full_model_name

                # Linux-Style Colored Prompt Formatting
                prompt_str_ansi = (
                    f"{self.cli.C_GREEN}{short_model_name}{self.cli.C_RESET}:"
                    f"{self.cli.C_BLUE}({branch_name}"
                    f"{self.cli.C_YELLOW}{dirty_flag}"
                    f"{self.cli.C_BLUE}){self.cli.C_RESET}"
                    f"{self.cli.C_GRAY}>{self.cli.C_RESET} "
                )

                # 3. ----- @par Read user input -----

                if HAS_PTK:
                    cmd_input = self.prompt_session.prompt(
                        ANSI(prompt_str_ansi),
                        completer=self._build_completer(),
                        complete_while_typing=False
                    ).strip()
                else:
                    cmd_input = input(prompt_str_ansi).strip()
                # End-if

                # Reset error counter because we successfully reached the blocking input layer
                consecutive_errors = 0

                if not cmd_input:
                    continue

                # 4. ----- @par Parse and Dispatch-----

                # Parse command.
                try:
                    parts = shlex.split(cmd_input)
                except ValueError as e:
                    self.cli.error(f"Shell syntax error: {e}")
                    continue
                # End-try

                # Split command.
                command = parts[0].lower()
                args = parts[1:]

                # Dispatch command.
                if command in ['help', '-h']:
                    self._print_help()
                elif command in ['quit', 'exit', '-q']:
                    self.cli.info("Terminating Dandelion Shell. Goodbye.")
                    break
                elif command == 'branch':
                    self._cmd_branch(args)
                elif command == 'checkout':
                    self._cmd_checkout(args)
                elif command == 'vim':
                    self._cmd_vim()
                elif command == 'load':
                    self._cmd_load(args)
                elif command == 'status':
                    self._cmd_status()
                elif command == 'commit':
                    self._cmd_commit()
                elif command == 'clear':
                    self.staged_message = ""
                    self.cli.success("Buffer cleared.")
                else:
                    self.cli.error(f"Unknown command '{command}'. Type 'help' for available commands.")
                # End-if
            # End-try

            # 5. ----- @par Exception Handle -----

            except KeyboardInterrupt:
                self.cli.raw("")
                continue
            except EOFError:
                self.cli.info("\nTerminating Dandelion Shell (EOF). Goodbye.")
                break
            except Exception as e:
                consecutive_errors += 1
                self.cli.error(f"\nUnexpected Error: {e}")

                # Break out if the loop is spinning wildly without user interaction
                if consecutive_errors >= 3:
                    self.cli.warning("FATAL: Too many consecutive errors. Terminating shell to prevent infinite loop.")
                    break

                self.cli.info("Shell recovered. Your staged message and session are preserved.")
                continue
        # End-while
    # End-def
# End-class