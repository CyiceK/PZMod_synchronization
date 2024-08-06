class I18n(object):
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = object.__new__(cls)
            cls._instance.print = print
        return cls._instance


if __name__ == '__main__':
    class A(I18n):
        def __init__(self):
            pass


    A().print("233333")
