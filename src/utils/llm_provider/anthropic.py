##
 # @file src/utils/llm_provider/anthropic.py
 # @date 2026/08/05
 # 
 # @brief Anthropic API Implement.
 #

from .base import LLMProvider

# Mapping from abstract effort level to Anthropic-compatible budget_tokens
# Used when thinking=enabled to control reasoning token budget
EFFORT_TO_BUDGET_TOKENS = {
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "max": 16384,
}
DEFAULT_EFFORT = "medium"

##
 # @brief Anthropic API Class.
 #
class AnthropicProvider(LLMProvider):
    ##
     # @brief Constructor.
     # 
     # @param api_key API key for the provider.
     # @param base_url Custom base URL (optional).
     # @param model_id Model identifier.
     # @param thinking "enabled" or "disabled" - whether to enable extended thinking.
     # @param effort Reasoning effort level: "low", "medium", "high", or "max".
     #
    def __init__(self, api_key, base_url, model_id, thinking="disabled", effort=DEFAULT_EFFORT):
        # Dynamic import.
        from anthropic import Anthropic

        # Construct request client (header).
        self.client = Anthropic(
            api_key=api_key,
            base_url=base_url if base_url else None,
            default_headers={
                "HTTP-Referer": "https://github.com/SwordofMorning/Dandelion",
                "X-Title": "Dandelion"
            }
        )
        self.model_id = model_id
        self.thinking = thinking
        self.effort = effort

        # @note Detect DeepSeek by base_url or model_id (case-insensitive)
        self._is_deepseek = (
            "deepseek" in (base_url or "").lower()
            or "deepseek" in model_id.lower()
        )
    # End-def

    ##
     # @brief Inject thinking configuration into payload when enabled.
     #
     # @param payload Request payload to be mutated in place (the payload sent to the LLM).
     #
     # @note Two formats are supported:
     # - Standard Anthropic: {"thinking": {"type": "enabled", "budget_tokens": N}}
     # - DeepSeek (Anthropic-compatible): {"output_config": {"effort": "low"|"medium"|"high"|"max"}}
     #   DeepSeek ignores budget_tokens; effort is the primary knob.
     # 
    def _inject_thinking(self, payload):
        if self.thinking == "enabled":
            if self._is_deepseek:
                # @note DeepSeek uses output_config.effort (like OpenAI's reasoning_effort).
                payload["output_config"] = {"effort": self.effort}
            else:
                budget = EFFORT_TO_BUDGET_TOKENS.get(self.effort, EFFORT_TO_BUDGET_TOKENS["medium"])
                payload["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": budget
                }
            # End-if
        # End-if
        # If thinking is "disabled", we intentionally do NOT add a thinking/output_config field.
    # End-def

    ##
     # @brief Non-streaming request.
     #
     # @param payload data send to LLM.
     # @param logger logger object, save log to file.
     # @param log_tag log tag saved in file.
     #
     # @return LLM's response and error.
     #
    def safe_request(self, payload, logger=None, log_tag=""):
        # 1. Patch payload: set model id and inject thinking.
        payload["model"] = self.model_id
        self._inject_thinking(payload)

        # 2. Save log.
        if logger and log_tag:
            logger.log_api_call(log_tag, payload)
        # End-if

        # 3. Request.
        try:
            resp = self.client.messages.create(**payload)
            return resp, None
        except Exception as e:
            return None, str(e)
        # End-try
    # End-def

    ##
     # @brief Streaming request.
     #
     # @param payload data send to LLM.
     # @param logger logger object, save log to file.
     # @param log_tag log tag saved in file.
     #
     # @return LLM's response and error.
     #
    def safe_stream_request(self, payload, logger=None, log_tag=""):
        # 1. Patch payload: set model id and inject thinking.
        payload["model"] = self.model_id
        self._inject_thinking(payload)

        # 2. Save log.
        if logger and log_tag:
            logger.log_api_call(log_tag, payload)

        # 3. Request.
        try:
            print("\n[Agent] ", end="", flush=True)
            with self.client.messages.stream(**payload) as stream:
                for event in stream:
                    # Print streaming string for user check in terminal.
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            # Print normal text.
                            print(event.delta.text, end="", flush=True)
                        elif event.delta.type == "input_json_delta":
                            # Print tool arguments in dark gray to show streaming progress.
                            print(f"\033[90m{event.delta.partial_json}\033[0m", end="", flush=True)
                        # End-if text_delta
                    # End-if content_block_delta
                # End-for streaming
            print()

            # Get final (full) message and return.
            final_message = stream.get_final_message()
            return final_message, None
        except Exception as e:
            print()
            return None, str(e)
        # End-try
    # End-def

    ##
     # @brief Extract plain text from response blocks.
     #
    def extract_text(self, content):
        if not isinstance(content, list):
            return str(content)
        return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")
    # End-def
# End-class