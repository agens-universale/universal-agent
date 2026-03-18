from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class AnswerQuestion(AbstractSimpleTool):
    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "answer_question",
            "description": "Outputs a direct answer to the user. Use this tool when you want to respond to the user "
                           "in natural language without performing any additional actions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "The full text of the answer that should be shown to the user."
                    }
                },
                "required": ["answer"],
                "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls):
        description = '''Tool name: answer_question
Tool description: Outputs a direct answer to the user in the console.
Input schema: {"type": "function", "name": "answer_question", "parameters": {"type": "object", "properties": {"answer": {"type": "string", "description": "The full text answer to show to the user."}}, "required": ["answer"], "additionalProperties": false}}

Usage:
- Use this tool when you want to respond directly to the user with a final answer or clarification.
- The provided text will be output to the console as-is (except for system-specific formatting).
- Prefer clear, concise, and helpful language in the user's preferred language.
'''
        return description

    def __call__(self, answer: str, **kwargs) -> str:
        if not isinstance(answer, str):
            return f"Error: Argument 'answer' must be a string. Got: {type(answer)} ({answer})"

        if not answer.strip():
            return "Error: Answer cannot be empty or whitespace only."

        print('\n\nAnswer:\n\n', answer, '\n\n')
        return 'Answer delivered to user'

