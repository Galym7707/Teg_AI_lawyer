# tools/preprocess_laws.py
# tiny wrapper чтобы workflow остался прежним и запускал normalize_laws.py
if __name__ == "__main__":
    # Импортируем main из tools.normalize_laws
    try:
        from tools.normalize_laws import main
    except Exception as e:
        # если импорт не сработал (запуск напрямую из каталога), попробуем относительный импорт
        import sys, os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.normalize_laws import main
    main()