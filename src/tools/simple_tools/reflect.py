import json

from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class Reflect(AbstractSimpleTool):
    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "reflect",
            "description": "Capture internal reflections, hypotheses, or insights about the task, environment, or solution strategy. These notes are stored in the conversation history to maintain context and trace your reasoning process. Use this whenever you need to analyze, question assumptions, or brainstorm before taking action.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The reflection content. Be specific: include assumptions, doubts, observations, or potential approaches. These notes are just drafts — their goal is to give you space to think, reflect, and reason about task execution. Feel free to use this tool freely for unstructured thinking."
                    }
                },
                "required": ["content"],
                "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls):
        description = '''Tool name: reflect
Tool description: Use it freely to generate any thinking or reasoning.'''
        return description

    def __call__(self, content: str, **kwargs) -> str:
        if not isinstance(content, str):
            raise ValueError(f"Argument 'content' must be a string. Got: {type(content)} ({content})")

        # print(f"****\nAgent current plan:\n\n{current_plan}\n\n****")

        return 'Reflection stored. Keeping this in mind for future reasoning.'


def run_test():
    tool = Reflect()
    assert tool(content='Conquer the World!') == 'Reflection stored. Keeping this in mind for future reasoning.'

    print("All tests passed.")

if __name__ == '__main__':
    run_test()
    arguments = {'content': '1. Определить структуру проекта: один HTML-файл с inline CSS и JavaScript.\n2. Создать HTML-страницу с canvas и интерфейсом выбора персонажа (Wall-E / Eva).\n3. Нарисовать простые спрайты для Wall-E и Eva с помощью canvas или создать как встроенные изображения (в виде данных).\n4. Закодировать лабиринт в виде двумерного массива (стены, проходы, точки, цветок).\n5. Реализовать основной игровой цикл: отрисовка, обработка ввода, движение игрока и врагов.\n6. Добавить логику сбора очков и проверку достижения цветка.\n7. Реализовать завершение уровня и отображение результата.\n8. Протестировать игру и при необходимости исправить ошибки.\n9. Подготовить итоговый отчет о выполнении.'}
    kwargs = {'run_agent_func': lambda : True, 'parent_state': dict()}
    tool = Reflect(**kwargs)
    print(tool(**arguments))



