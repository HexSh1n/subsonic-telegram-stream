"""Первый вход в Telegram: python -m app.login

Создаёт файл сессии. Показывает способ доставки кода; пустой Enter
в запросе кода — повторная отправка (переключает App -> SMS -> звонок).
"""
import asyncio


NAMES = {"SentCodeTypeApp": "в приложение Telegram (чат от 'Telegram')",
         "SentCodeTypeSms": "по SMS",
         "SentCodeTypeCall": "звонком (код продиктуют)",
         "SentCodeTypeFlashCall": "flash-звонком",
         "SentCodeTypeMissedCall": "пропущенным звонком",
         "SentCodeTypeSetUpEmailRequired": "!! Telegram требует привязать e-mail, код не шлёт",
         "SentCodeTypeEmailCode": "на привязанный e-mail",
         "SentCodeTypeFirebaseSms": "Firebase-SMS (только для Google-устройств)"}


def _describe(sent) -> str:
    t = NAMES.get(type(sent.type).__name__, type(sent.type).__name__)
    nxt = getattr(sent, "next_type", None)
    nxt = NAMES.get(type(nxt).__name__, "-") if nxt else "-"
    timeout = getattr(sent, "timeout", "?")
    return (f"Telegram ответил: код отправлен [{t}]; "
            f"при повторе будет: {nxt}; повтор доступен через {timeout} сек")


async def main() -> None:
    from telethon import errors
    from telethon.tl.functions.auth import ResendCodeRequest

    from .config import load_config
    from .telegram.client import build_telethon_client

    cfg = load_config()
    client = build_telethon_client(cfg)
    await client.connect()

    phone = input("Телефон в межд. формате (+79991234567): ").strip()
    sent = await client.send_code_request(phone)
    print(_describe(sent))

    while True:
        code = input("Код (пустой Enter — выслать повторно): ").strip()
        if not code:
            try:
                sent = await client(ResendCodeRequest(phone=phone,
                                                      phone_code_hash=sent.phone_code_hash))
                print("Отправлено повторно.", _describe(sent))
            except errors.FloodWaitError as e:
                print(f"Слишком часто. Жди {e.seconds} сек и попробуй снова.")
            except Exception as e:
                print("Повторная отправка не сработала:", type(e).__name__, e)
            continue
        try:
            await client.sign_in(phone=phone, code=code)
            break
        except errors.SessionPasswordNeededError:
            while True:
                pw = input("Пароль двухфакторной аутентификации: ")
                try:
                    await client.sign_in(password=pw)
                    break
                except Exception as e:
                    print("Пароль не принят:", e)
            break
        except errors.PhoneCodeInvalidError:
            print("Неверный код, попробуй ещё раз (самый свежий!).")
        except Exception as e:
            print("Ошибка входа:", e)

    me = await client.get_me()
    print(f"Готово: авторизован как {getattr(me, 'username', None) or me.first_name}.")
    print("Теперь вписывай канал в config.yaml и запускай: python -m app")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
