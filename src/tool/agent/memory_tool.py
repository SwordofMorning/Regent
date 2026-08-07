# src/tool/agent/memory_tool.py

from ..base_tool import BaseTool

class MemoryTool(BaseTool):
    def __init__(self, memory_manager):
        super().__init__()
        self.memory = memory_manager

    def get_name(self):
        return "remember"

    def get_description(self):
        return (
            "Save important facts, user preferences, or architectural decisions to memory. "
            "Memory is two-tier: use scope='global' (default) for project-wide knowledge "
            "preserved across sessions (coding style, architecture decisions, preferences), "
            "or scope='session' for facts that only apply to the current session branch. "
            "Use this when you learn something that should not be forgotten."
        )

    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique short name for the memory topic (e.g., 'coding_style')."},
                "description": {"type": "string", "description": "One sentence summary of what this memory is about."},
                "tags": {"type": "string", "description": "Comma separated tags (e.g., 'preference, python')."},
                "content": {"type": "string", "description": "The detailed content to remember."},
                "scope": {
                    "type": "string",
                    "enum": ["global", "session"],
                    "description": "Storage scope: 'global' (project-wide, default) or 'session' (current session branch only)."
                }
            },
            "required": ["name", "description", "content"]
        }

    def execute(self, **kwargs):
        name = kwargs.get("name")
        description = kwargs.get("description", "")
        tags = kwargs.get("tags", "general")
        content = kwargs.get("content", "")
        # Treat an explicit null scope the same as an omitted one (default global).
        scope = str(kwargs.get("scope") or "global").strip().lower()

        if not name or not content:
            return False, "Error: name and content are required."

        if scope not in ("global", "session"):
            return False, f"Error: scope must be 'global' or 'session', got '{scope}'."

        # ASCII-only enforcement (Language Policy): internal storage must stay
        # ASCII so keyword retrieval (space-split) keeps working. Chinese or
        # other non-ASCII values are rejected with a clear hint to translate.
        non_ascii = [
            v for v in (name, description, tags, content)
            if any(ord(ch) > 127 for ch in str(v or ""))
        ]
        if non_ascii:
            return False, (
                "Error: 'remember' stores ASCII-only content (non-ASCII text breaks "
                "keyword retrieval). Please translate the following values to English "
                f"and re-submit: {non_ascii}"
            )

        try:
            success = self.memory.write_memory(name, description, tags, content, scope=scope)
        except ValueError as e:
            # Reserved names (e.g. MEMORY -> MEMORY.md collision), sanitized-name
            # collisions, overlong names and unverifiable existing files surface
            # as a tool error instead of crashing the turn.
            return False, str(e)
        except OSError as e:
            # Disk-level failures (ENAMETOOLONG survivors, permission errors,
            # full disk) must never crash the agent turn.
            return False, f"Error: failed to write memory file: {e}"
        if success:
            # Echo the stored memory back (content truncated) so the model
            # sees it immediately in the tool result. Mid-tool-loop the
            # [System: Dynamic Context] block is only refreshed at user turns,
            # so this echo keeps the new memory visible without re-injecting
            # into the messages prefix (cache-friendly).
            content_preview = content[:500] + ("..." if len(content) > 500 else "")
            return True, (
                f"Successfully saved {scope} memory topic '{name}'.\n"
                f"Description: {description}\n"
                f"Tags: {tags}\n"
                f"Content: {content_preview}"
            )
        return False, "Failed to save memory."
