"""Вход по QR-коду (без SMS-кода): python -m app.login_qr

На телефоне, где Telegram уже залогинен твоим номером:
Настройки -> Устройства -> Подключить устройство -> наведи камеру на QR.
"""
import asyncio


async def main() -> None:
    import qrcode
    from telethon import errors

    from .config import load_config
    from .telegram.client import build_telethon_client

    cfg = load_config()
    client = build_telethon_client(cfg)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print("Уже авторизован:", getattr(me, "username", None) or me.first_name)
        await client.disconnect()
        return

    qr = await client.qr_login()
    authorized = False
    while not authorized:
        print()
        print("Открой на телефоне: Telegram -> Настройки -> Устройства ->")
        print("Подключить устройство (Link Desktop Device) и отсканируй QR ниже.")
        print()
        console = qrcode.QRCode(border=2)
        console.add_data(qr.url)
        try:
            console.print_ascii(tty=True)
        except Exception:
            console.print_ascii()
        print()

        try:
            await qr.wait(timeout=150)
            authorized = True
        except asyncio.TimeoutError:
            qr = await qr.recreate()
            print("QR истёк, рисую новый...")
        except errors.SessionPasswordNeededError:
            while True:
                pw = input("Пароль двухфакторной аутентификации: ")
                try:
                    await client.sign_in(password=pw)
                    authorized = True
                    break
                except Exception as e:
                    print("Пароль не принят:", e)

    me = await client.get_me()
    print(f"Готово: авторизован как {getattr(me, 'username', None) or me.first_name}.")
    print("Теперь: канал(ы) в config.yaml -> запуск сервера: python -m app")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
