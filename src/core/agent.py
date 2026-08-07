##
 # @file src/core/agent.py
 # @date 2026/08/06
 # 
 # @brief Agent-Loop and others helper functions.
 #
 # @note Agent runtime call chain:
 #   CLI interactive loop
 #     -> MyAgent.step()                 # LLM Round-Trip Step (tools iterator)
 #     -> _compact_context()             # token budget check + LLM summary
 #     -> _get_memories()                # memory injection (cached)
 #     -> SafeLLMClient.safe_stream_request()
 #     -> tool handlers (memory/state/fs/...)
 #     -> history append (tool_result)   # loop continues until stop_reason != tool_use
 #

import os
import re
import sys
import json
import datetime

from src.utils import SafeLLMClient
from src.utils import CLIPrinter
from src.core.memory import MemoryManager
from src.core.skill import SkillManager
from src.core.sysprompt import PromptBuilder
from src.subagent import SubAgentPool

from src.tool import (
    BashTool, LoadSkillTool, MarkdownTool,
    GrepSearchTool, WriteFileTool, ReadFileTool, ListDirectoryTool,
    EditFileTool, PlanTool, SpawnSubagentTool, WebSearchTool,
    ReadExcelTool, WriteExcelTool,
    StateTool, MemoryTool
)

# Create a module-level CLIPrinter instance for convenience
cli = CLIPrinter()

# Dynamic Context injection markers: the [System: Dynamic Context] block is
# appended to the newest plain-text user message (fresh region) instead of the
# system prompt, so the system prompt stays byte-identical for the whole
# session and DeepSeek's prefix cache keeps hitting across tool-loop iterations.
_DYN_CTX_START = "[System: Dynamic Context"
_DYN_CTX_END = "[System: Dynamic Context End]"

##
 # @brief Strip a previously injected [System: Dynamic Context] block from a
 #        user message content string.
 #
 # @note Defensive: normally the target message is brand new (just added by
 #       inject_user_message) and contains no block. Used on resume/re-run
 #       when the same message may already carry a stale block, so a new
 #       injection replaces (instead of stacking on) the old one.
 #
 # @param content User message content string.
 #
 # @return Content with the injected block removed (trailing whitespace kept).
 #
def strip_dynamic_context(content):
    start = content.find(_DYN_CTX_START)
    if start == -1:
        return content
    end = content.find(_DYN_CTX_END, start)
    if end == -1:
        # Unterminated block (e.g. manually truncated history): drop the tail.
        return content[:start].rstrip()
    return content[:start].rstrip() + content[end + len(_DYN_CTX_END):]
# End-def

##
 # @brief Agent Loop Wrapper Class.
 #
class MyAgent:
    ##
     # ========================================
     # @section I. Constructor and Init.
     # Construct MyAgent obj, and init all tools.
     # ========================================
     #

    ##
     # @brief Constructor.
     #
     # @param config api.cfg loaded from .env/.
     # @param session_manager SessionManager object.
     # @param workspace_dir current pwd, used to avoid agent(llm) escape.
     #
    def __init__(self, config, session_manager, workspace_dir):
        # ----- @par 1. Init members -----

        # Alignment members.
        self.config = config
        self.session = session_manager
        self.workspace_dir = workspace_dir
        # Clear error counts.
        self.error_count = 0
        # Inject thinking level.
        self.thinking = str(config.get("THINKING", "disabled")).strip().lower()
        self.effort = str(config.get("EFFORT", "medium")).strip().lower()

        # Load history from the current session
        self.history = self.session.load_history()

        # ----- @par 2. Init Subsystem -----

        # Init request client with absolute paths.
        self.client = SafeLLMClient(
            api_key=self.config["ANTHROPIC_API_KEY"],
            base_url=self.config["ANTHROPIC_BASE_URL"],
            model_id=self.config["MODEL_ID"],
            sdk_type=self.config.get("SDK_TYPE", "Anthropic"),
            all_models=self.config.get("ALL_MODELS", []),
            sub_list=self.config.get("SUB_LIST", []),
            thinking=self.thinking,
            effort=self.effort,
            logger=self.session
        )

        # In passing session_manager as logger to maintain compatibility with legacy code.
        # Memory is two-tier: global (llm/memory/) + current session (.log/sess_<id>/memory/).
        # The session tier resolves dynamically via session_manager so `checkout` switches memory scope without a rebuild.
        self.memory = MemoryManager(
            memory_dir=os.path.join(self.workspace_dir, "llm", "memory"),
            session_manager=self.session,
            safe_client=self.client,
            logger=self.session
        )
        self.skill = SkillManager(
            skill_dir=os.path.join(self.workspace_dir, "llm", "skill")
        )
        self.prompt_builder = PromptBuilder(self.memory, self.skill, self.config, self.workspace_dir,
                                            session_manager=self.session)

        # Memories cache: refresh only when the last plain-text user message changes,
        # so the tail of system_prompt stays stable during tool loops (cache-friendly).
        self._memories_key = None
        self._memories_cache = ""
        # Last built system prompt, reused by _soft_token_limit() so the token
        # budget accounts for the real request overhead without rebuilding.
        self._last_system_prompt = ""

        # ----- @par 3. Load Tools -----

        # Init tools for Main Agent.
        self._init_tools()
    # End-def

    ##
     # @brief Init tools for Main Agent and Subagents (pool).
     #
    def _init_tools(self):
        # ----- @par 1. Create Tools Object -----

        self.tools = {}
        # Pass BASE_DIR to all file-system related tools
        # Bash maintains its own command checking
        bash = BashTool(workspace_dir=self.workspace_dir)
        skill_loader = LoadSkillTool(self.skill)
        # Editor
        md_editor = MarkdownTool(workspace_dir=self.workspace_dir)
        read_excel_tool = ReadExcelTool(workspace_dir=self.workspace_dir)
        write_excel_tool = WriteExcelTool(workspace_dir=self.workspace_dir)
        # FS
        grep_tool = GrepSearchTool(workspace_dir=self.workspace_dir)
        write_tool = WriteFileTool(workspace_dir=self.workspace_dir)
        read_tool = ReadFileTool(workspace_dir=self.workspace_dir)
        list_tool = ListDirectoryTool(workspace_dir=self.workspace_dir)
        edit_tool = EditFileTool(workspace_dir=self.workspace_dir)
        # Others
        web_search_tool = WebSearchTool(workspace_dir=self.workspace_dir, config=self.config)
        # Memory
        state_tool = StateTool(workspace_dir=self.workspace_dir, session_manager=self.session)
        memory_tool = MemoryTool(self.memory)

        # Create full tools mapping for Orchestrator
        all_tools = {
            bash.get_name(): bash,
            skill_loader.get_name(): skill_loader,
            md_editor.get_name(): md_editor,
            grep_tool.get_name(): grep_tool,
            write_tool.get_name(): write_tool,
            read_tool.get_name(): read_tool,
            list_tool.get_name(): list_tool,
            edit_tool.get_name(): edit_tool,
            web_search_tool.get_name(): web_search_tool,
            read_excel_tool.get_name(): read_excel_tool,
            write_excel_tool.get_name(): write_excel_tool
        }

        # ----- @par 2. Subagent Pool and Tools -----

        self.pool = SubAgentPool(
            safe_client=self.client,
            logger=self.session,
            config=self.config,
            all_tools=all_tools,
            max_depth=int(self.config.get("MAX_SUBAGENT_DEPTH", 3))
        )

        # Decompose one descriptions to multi (or one) tasks.
        plan_tool = PlanTool(self.client, self.config)
        # Spawn a new subagent.
        spawn_subagent = SpawnSubagentTool(self.pool)

        # ----- @par 3. Register Tools  -----

        # Added all tools to the registration list
        tool_list = [
            bash, skill_loader, md_editor,
            grep_tool, write_tool, read_tool, list_tool,
            edit_tool, plan_tool, spawn_subagent, web_search_tool,
            read_excel_tool, write_excel_tool,
            state_tool, memory_tool
        ]

        for t in tool_list:
            self.tools[t.get_name()] = t

        self.tool_schemas = [
            {
                "name": t.get_name(),
                "description": t.get_description(),
                "input_schema": t.get_schema()
            } for t in self.tools.values()
        ]
    # End-def

    ##
     # ========================================
     # @section II. Message Helper Functions.
     # ========================================
     #

    ##
     # @brief A user message that is plain text (not a tool_result payload).
     #
     # @return True or False.
     # @retval True is user input msg;.
     # @retval False is not user input msg.
     #
    @staticmethod
    def _is_plain_user_msg(msg):
        if msg.get("role") != "user":
            return False
        content = msg.get("content", "")
        if isinstance(content, list):
            return not any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
        return True
    # End-def

    ##
     # @brief True if an assistant message ends with a tool_use block (handles both
     # dict blocks loaded from history.log and SDK objects in memory).
     #
     # @return True of False.
     # @retval True is end with tool_use.
     # @retval False is not end with tool_use.
     #
    @staticmethod
    def _msg_ends_with_tool_use(msg):
        if msg.get("role") != "assistant":
            return False
        content = msg.get("content", "")
        if not isinstance(content, list) or not content:
            return False
        last = content[-1]
        if isinstance(last, dict):
            return last.get("type") == "tool_use"
        return getattr(last, "type", None) == "tool_use"
    # End-def

    ##
     # @brief Trim head so it never ends with an assistant tool_use message.
     # The trimmed tool_use message stays in middle (summarized) together with its
     # matching tool_result, so the summary insertion can never split a pair.
     #
     # @param history Full message history.
     # @param head_size Desired head size before trimming.
     #
     # @return Trimmed head list (never ends with an assistant tool_use).
     #
    @staticmethod
    def _trim_head_for_tool_use(history, head_size):
        head = history[:head_size]
        while head and MyAgent._msg_ends_with_tool_use(head[-1]):
            head = head[:-1]
        return head
    # End-def

    ##
     # @brief Append-only archive filename: history length + timestamp,
     # so a second compaction at the same history length never overwrites the first.
     #
     # @param archive_dir Session archives directory.
     # @param history_len History length at compaction time.
     #
     # @return Absolute path like <archive_dir>/history_<len>_<timestamp>.json.
     #
    @staticmethod
    def _archive_path(archive_dir, history_len):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return os.path.join(archive_dir, f"history_{history_len}_{ts}.json")
    # End-def

    ##
     # @brief Absolute artifact path (resolvable by read_file
     # even when the process CWD differs from the workspace/session directory).
     #
     # @param session_dir Current session directory.
     # @param block_id Tool_use block id (sanitized into the filename).
     #
     # @return Absolute path of the offloaded artifact file.
     #
    @staticmethod
    def _artifact_path(session_dir, block_id):
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(block_id))
        return os.path.abspath(os.path.join(session_dir, "artifacts", f"{safe_id}.txt"))
    # End-def

    ##
     # ========================================
     # @section III. Context Compaction.
     # token-aware, LLM-summarized, pair-safe
     # ========================================
     #

    ##
     # @brief Heuristic token estimate: ASCII ~4 chars/token, CJK ~1.5 chars/token.
     #
     # @param history Message list to estimate; defaults to self.history.
     #
     # @return Estimated token count (float).
     #
    def _estimate_tokens(self, history=None):
        history = history if history is not None else self.history
        ascii_chars = 0
        non_ascii_chars = 0
        for m in history:
            content = m.get("content", "")
            if isinstance(content, list):
                content = str(content)
            for ch in str(content):
                if ord(ch) < 128:
                    ascii_chars += 1
                else:
                    non_ascii_chars += 1
                # End-if
            # End-for
        # End-for
        return ascii_chars / 4.0 + non_ascii_chars / 1.5
    # End-def

    ##
     # @brief History-only token budget: MAX_CONTEXT_TOKENS minus 
     # the fixed request overhead (system prompt + tool schemas).
     #
     # @note The system prompt already includes the memories tail
     # (_last_system_prompt is built by appending memories_content in step()),
     # so memories must NOT be counted twice here.
     # @note Compaction triggered at this limit keeps the COMBINED provider request
     # within MAX_CONTEXT_TOKENS instead of silently overflowing it.
     #
     # @return int: MAX_CONTEXT_TOKENS minus request overhead (>= 1).
     #
    def _soft_token_limit(self):
        base = int(self.config.get("MAX_CONTEXT_TOKENS", 128000))
        overhead = self._estimate_tokens([
            {"role": "user", "content": self._last_system_prompt or self.prompt_builder.build()},
            {"role": "user", "content": json.dumps(self.tool_schemas, ensure_ascii=False)},
        ])
        return max(int(base - overhead), 1)
    # End-def

    ##
     # @brief Compact context and memory save.
     #
    def _compact_context(self):
        est_tokens = self._estimate_tokens()
        soft_limit = self._soft_token_limit()

        ## 
         # @brief The token budget is the SINGLE compaction switch: 
         # when history alone is already at/over the soft limit,
         # compaction must run regardless of history length (chat turns).
         #
         # @note A short-history bypass here (e.g. len < 20) would let
         # the request overflow MAX_CONTEXT_TOKENS (and provider rejection) in
         # setups with a large system prompt + tool schemas. Short-history
         # handling lives INSIDE the compaction flow below (head+summary-only
         # fallback), never before the budget check.
        if est_tokens < soft_limit:
            return

        print(f"[*] Context limit reached (~{int(est_tokens)} tokens), compacting history via LLM...")

        # ----- @par 1. Backup -----

        # Full archive backup (append-only, restorable)
        archive_dir = os.path.join(self.session.current_session_dir, "archives")
        os.makedirs(archive_dir, exist_ok=True)
        archive_path = self._archive_path(archive_dir, len(self.history))
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2,
                      default=self.session._default_serializer)

        head_size = 5
        recent_size = 15

        ## 
         # @brief Trim head so it never ends with an assistant tool_use message.
         #
         # @note The summary (role=user) is inserted right after head, and a trailing
         # tool_use with no matching tool_result would corrupt the pairing.
         #
        head = self._trim_head_for_tool_use(self.history, head_size)
        trimmed_head_size = len(head)

        # ----- @par 2. Context Window -----

        ## @note Find a safe start for the recent window: the latest plain-text user
         # @note message within the look-back limit. Starting at a plain-text user;
         # @note message guarantees tool_use/tool_result pairs are never split.
        max_lookback = min(len(self.history) - trimmed_head_size, recent_size * 2)
        start_idx = None
        for i in range(len(self.history) - 1, len(self.history) - 1 - max_lookback, -1):
            if self._is_plain_user_msg(self.history[i]):
                start_idx = i
                break
            # End-if
        # End-for

        if start_idx is None or start_idx < trimmed_head_size:
            # Fallback: no usable plain-text user message outside the head
            # window; keep only head + summary to avoid dangling or duplicated
            # tool_result blocks.
            print("[-] No safe compaction breakpoint found; keeping head + summary only.")
            start_idx = len(self.history)
        # End-if

        recent = self.history[start_idx:]
        middle = self.history[trimmed_head_size:start_idx]

        # ----- @par 3. Summarize -----

        # Summarize head + middle (early goals are the most drift-prone part)
        summary_src = head + middle
        summary_text = json.dumps(summary_src, ensure_ascii=False, indent=2,
                                  default=self.session._default_serializer)
        if len(summary_text) > 200000:
            # Keep the head (goals/decisions) and the tail; drop the middle body.
            summary_text = (summary_text[:50000]
                            + "\n...[middle omitted from summarization input]...\n"
                            + summary_text[-150000:])
        # End-if

        summary_prompt = (
            "Please summarize the following conversation history.\n"
            "Focus on:\n"
            "1. <goals>: Current tasks and acceptance criteria.\n"
            "2. <completed>: What has been done so far.\n"
            "3. <decisions>: Key technical decisions and reasons.\n"
            "4. <artifacts>: Key file paths, variable names, or error codes.\n"
            "5. <pending>: What still needs to be done.\n\n"
            "Output strictly in XML format using the tags above."
        )

        summary_payload = {
            "messages": [{"role": "user", "content": summary_prompt + "\n\nHistory:\n" + summary_text}],
            "max_tokens": 2000,
            "system": "You are a concise memory summarization AI."
        }

        # ----- @par 4. Request -----

        resp, err = self.client.safe_request(summary_payload, log_tag="COMPRESSION SUMMARY")
        if err:
            print(f"[-] Compression failed: {err}. Falling back to basic snip.")
            summary_content = "[Compression Failed. History snipped.]"
        else:
            summary_content = self.client.extract_text(resp.content)

        summary_msg = {
            "role": "user",
            "content": (f"[System: Context compacted at {archive_path}]\n\n"
                        f"<conversation_summary>\n{summary_content}\n</conversation_summary>")
        }

        self.history = head + [summary_msg] + recent
        self.session.save_history(self.history)

        # Invalidate memories cache: history changed (plain-text user messages may shift).
        self._invalidate_memories_cache()
        print("[+] Context compacted successfully.")

        # ----- @par 5. Post -----

        # Post-compaction guard: if the budget is still exceeded (e.g. the
        # configured MAX_CONTEXT_TOKENS is below the system-prompt + tools
        # overhead), warn loudly once per compaction instead of letting every
        # subsequent step re-trigger an LLM summarization call in a loop.
        remaining = self._estimate_tokens()
        if remaining >= soft_limit:
            print(f"[-] Warning: history still ~{int(remaining)} tokens after "
                  f"compaction (soft limit ~{int(soft_limit)}). Consider raising "
                  "MAX_CONTEXT_TOKENS or reducing system-prompt/tool overhead.")
    # End-def _compact_context

    ##
     # @brief Drop both memory cache fields so the next _get_memories() call
     # reloads persisted memories instead of returning a stale value.
     #
    def _invalidate_memories_cache(self):
        self._memories_key = None
        self._memories_cache = ""
    # End-def

    ##
     # @brief Load relevant memories, cached until the last plain-text user message changes.
     #
     # @return Memory string cached until the last plain user message changes;
     #          ""(empty) when no relevant memory found.
     #
    def _get_memories(self):
        key = None
        for i in range(len(self.history) - 1, -1, -1):
            msg = self.history[i]
            if self._is_plain_user_msg(msg):
                key = (i, hash(str(msg.get("content", ""))[:2000]))
                break
        # End-for
        if key is not None and key == self._memories_key:
            return self._memories_cache
        self._memories_key = key
        self._memories_cache = self.memory.load_memories_string(self.history)
        return self._memories_cache
    # End-def

    ##
     # @brief Render the [System: Dynamic Context] block: memory index +
     #        relevant memories digest + task state (Attention Anchor).
     #
     # @note The block is appended to the newest plain-text user message
     #       (fresh region) instead of the system prompt, so the system prompt
     #       stays byte-identical for the whole session (prefix caching).
     #
     # @return Block text starting with the [System: ...] header, or "" when
     #         there is nothing dynamic to inject (no state file & no memories).
     #
    def _render_dynamic_context(self):
        sections = []

        # 1. Memory index (global + session tiers).
        index = self.memory.get_index_text()
        if index:
            sections.append(f"Relevant Memories:\n{index}")

        # 2. Relevant memories digest (<relevant_memories> style).
        memories_content = self._get_memories()
        if memories_content:
            sections.append(memories_content)

        # 3. Task State (Attention Anchor), session-scoped. Kept LAST so the
        #    anchor sits closest to the model's next output position.
        state_file = None
        if self.session is not None:
            state_file = self.session.ensure_task_state_file()
        if state_file and os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                if isinstance(state, dict):
                    session_hint = ""
                    if getattr(self.session, "current_session_id", None):
                        session_hint = f" (session: {self.session.current_session_id})"
                    sections.append(
                        PromptBuilder.render_task_state_text(state, session_hint))
                # End-if
            except Exception as e:
                # Log failure details instead of silently dropping the section.
                print(f"[-] Warning: Failed to load task state from {state_file}: {e}")
            # End-try
        # End-if

        if not sections:
            return ""
        return (f"{_DYN_CTX_START} (auto-injected, not user input)]\n"
                + "\n\n".join(sections)
                + f"\n{_DYN_CTX_END}")
    # End-def

    ##
     # @brief Append the dynamic context block to the newest plain-text user
     #        message and persist it, so tool-loop iterations (which only read
     #        history) keep seeing it inside the stable messages prefix.
     #
     # @note The target message has just been added by inject_user_message()
     #       and has never been sent, so mutating it costs zero cache.
     #
    def _inject_dynamic_context(self):
        block = self._render_dynamic_context()
        if not block:
            return
        msg = self.history[-1]
        content = msg.get("content", "")
        if isinstance(content, str) and _DYN_CTX_START in content:
            # Defensive: replace a stale block (resume/re-run) instead of
            # stacking a second one on the same message.
            content = strip_dynamic_context(content)
        # End-if
        msg["content"] = content + "\n\n" + block
        self.session.save_history(self.history)
    # End-def

    ##
     # ========================================
     # @section IV. Agent-Loop
     # ========================================
     #

    ##
     # @brief LLM Round-Trip Step. A tools iterator.
     #
     # @note This function only one round chat:
     # Compact Context -> Inject Memory -> Request LLM -> Execute All Tools -> Return.
     #
     # @note Agent-loop is held by CLI:
     # CLI -> agent.step() -> Request LLM -> Execute All Tools -> Return ->
     # CLI (continue? or stop?) -> agent.step() | Stop in CLI
     #
     # @see src/utils/cli/interactive_cli.py
     #
     # @return True: continue the loop; False: stop.
     # @retval True This round executed a tool call (or unexpected), need to feed back result to LLM. Continue.
     # @retval False This round is a plain text reply (or an API error). Breakout.
     #
    def step(self):
        # 0. Check context budget every turn (not only on user messages).
        self._compact_context()

        # 1. Build System Prompt (STATIC)
        # Dynamic content (task state / memories) is injected as a
        # [System: Dynamic Context] block appended to the newest plain-text
        # user message (see _inject_dynamic_context), so the system prompt
        # stays byte-identical for the whole session -> prefix cache hits.
        system_prompt = self.prompt_builder.build()
        self._last_system_prompt = system_prompt

        # 1.1 Dynamic Context Injection
        # Only at a new user turn: the newest history message is a plain-text
        # user message that has never been sent, so appending the block costs
        # zero cache. Tool-loop iterations (newest message = tool_result)
        # never re-inject; the block persists in history and stays inside the
        # stable messages prefix.
        if self.history and self._is_plain_user_msg(self.history[-1]):
            self._inject_dynamic_context()
        # End-if

        # Pure append-only copy, ZERO mutations.
        req_messages = self.history.copy()

        # 2. Main LLM API Call
        payload = {
            "tools": self.tool_schemas,
            "messages": req_messages,
            "max_tokens": int(self.config["MAX_TOKENS"]),
            "system": system_prompt
        }

        # PRE-call logging is now handled inside SafeLLMClient -> Provider
        # (after thinking injection), so we only log POST here.

        # Streaming
        resp, err = self.client.safe_stream_request(payload)

        # POST-call logging
        self.session.log_api_call("POST LLM CALL - MAIN", resp if resp else {"error": err})

        if err is not None:
            print(f"[-] API Error: {err}")
            return False

        self.history.append({"role": "assistant", "content": resp.content})
        self.session.save_history(self.history)

        # 3. Handle Output or Tools
        if resp.stop_reason != "tool_use":
            return False

        # Handle Tools
        results = []

        # Tools Iterator.
        for block in resp.content:
            if block.type != "tool_use":
                continue

            cli.print(f"\nTool requested: {block.name}", level="info")
            handler = self.tools.get(block.name)

            if handler:
                success, output = handler.execute(**block.input)
                # A successful memory write changes what _get_memories() would
                # load for the next tool-loop iteration; drop the cache so the
                # system prompt tail reflects the newly persisted memory.
                # Failed executions keep the previous cache untouched.
                if success and handler.get_name() == "remember":
                    self._invalidate_memories_cache()
            else:
                success, output = False, f"Unknown tool: {block.name}"

            output_str = str(output)
            cli.print(f"    Result length: {len(output_str)} chars", level="debug")
            
            # --- Large Output Offload ---
            # Prevents context explosion and delays the need for compression.
            # Threshold is intentionally low (8K chars ~ 2-4K tokens): outputs
            # beyond this are archived to disk and replaced with a truncated
            # pointer so the model can read_file the missing parts on demand.
            MAX_INLINE_CHARS = 8000
            if len(output_str) > MAX_INLINE_CHARS:
                # Absolute path: the truncated pointer is resolved by read_file
                # relative to the workspace, so it must not depend on the CWD.
                artifact_path = self._artifact_path(self.session.current_session_dir, block.id)
                os.makedirs(os.path.dirname(artifact_path), exist_ok=True)

                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(output_str)

                trunc_output = output_str[:MAX_INLINE_CHARS]
                trunc_output += (
                    f"\n\n... [OUTPUT TRUNCATED. Full {len(output_str)} chars output "
                    f"saved to {artifact_path}. Use read_file to read specific missing parts.]"
                )
                output_str = trunc_output

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output_str})
        # End-for Agent-Loop

        if results:
            self.history.append({"role": "user", "content": results})
        else:
            self.history.append({"role": "user", "content": "You indicated a tool use but provided no valid tool calls."})

        self.session.save_history(self.history)
        return True
    # End-def

    ##
     # @brief Append a user text message to history and run a context budget check.
     #
     # @param text User input text to append to history.
     #
    def inject_user_message(self, text):
        self.history.append({"role": "user", "content": text})
        self.session.save_history(self.history)
        self._compact_context()
    # End-def

    ##
     # @brief Reload history when session changed.
     #
    def reload_history(self):
        self.history = self.session.load_history()
        # Session switched: memory relevance cache must be recomputed because
        # the session tier (and possibly the whole history) changed.
        self._memories_key = None
        self._memories_cache = ""
        # The cached system prompt was built from the PREVIOUS session's task
        # state and memory index. Drop it so the next token-budget estimate
        # (_soft_token_limit) rebuilds from the new session instead of
        # reusing stale overhead from the old branch.
        self._last_system_prompt = ""
        # NOTE: previously injected [System: Dynamic Context] blocks inside
        # history are intentionally NOT stripped here: keeping the messages
        # prefix byte-identical lets the server-side prefix cache survive a
        # session resume. Stale blocks are harmless (the newest injected
        # block always carries the latest state) and compaction removes them.
    # End-def
# End-class