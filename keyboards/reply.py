from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

base = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="❤️Купить подписку")
        ],
        [
            KeyboardButton(text="👤Информация"),
            KeyboardButton(text="🧑‍💻Поддержка")
        ]
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие из меню",
    selective=True
)