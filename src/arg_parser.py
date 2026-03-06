import argparse

run_args = dict()

parser = argparse.ArgumentParser(
    description="CLI-агент для взаимодействия с AI-сервером",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""
Примеры использования:
  agent -q "напиши функцию сортировки списка"
  agent --query "как оптимизировать этот код?" --verbose
        """
)

parser.add_argument(
    "-q", "--query",
    type=str,
    required=True,
    help="Запрос к агенту (обязательный параметр)"
)

parser.add_argument(
    "-v", "--verbose",
    action="store_true",
    help="Выводить подробную информацию о запросе"
)

parser.add_argument(
    "--config",
    type=str,
    help="Путь к кастомному конфигурационному файлу"
)

parser.add_argument(
    "-t", "--thread-id",
    type=str,
    help="Идентификатор треда для продолжения диалога"
)


run_args = parser.parse_args()

if __name__ == '__main__':
    print(run_args)
