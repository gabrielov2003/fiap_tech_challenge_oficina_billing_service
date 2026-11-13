import application
from app import injetar_dependencias
from observabilidade import configurar_logs


def main():
    configurar_logs()
    injetar_dependencias()
    while True:
        application.processar_comandos()


if __name__ == '__main__':
    main()
