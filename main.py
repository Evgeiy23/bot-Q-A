import asyncio
import logging
import json
import uuid
import qrcode
import io
import base64
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum

from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Конфигурация
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # Замените на ваш токен бота от @BotFather
WEBHOOK_URL = "https://your-domain.com"  # Для QR-кодов (опционально)

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Enums
class UserRole(Enum):
    TEACHER = "teacher"
    STUDENT = "student"

class QuestionType(Enum):
    MULTIPLE_CHOICE = "multiple_choice"
    TEXT_INPUT = "text_input"

# Состояния FSM
class TeacherStates(StatesGroup):
    waiting_question_count = State()
    creating_question = State()
    waiting_question_type = State()  # New state for question type selection
    waiting_question_text = State()
    waiting_question_text_after_photo = State()  # New state for text after photo
    waiting_photo = State()
    waiting_options = State()
    waiting_correct_answer = State()

class StudentStates(StatesGroup):
    taking_test = State()
    answering_question = State()

# Модели данных
@dataclass
class Question:
    id: str
    text: str
    question_type: QuestionType
    options: Optional[List[str]] = None
    correct_answer: str = ""
    photo_file_id: Optional[str] = None  # Для хранения ID фото в Telegram

@dataclass
class Test:
    id: str
    teacher_id: int
    teacher_username: str
    questions: List[Question]
    created_at: datetime
    name: str = ""
    active: bool = True

@dataclass
class StudentAnswer:
    question_id: str
    answer: str
    is_correct: bool
    skipped: bool = False

@dataclass
class TestResult:
    test_id: str
    student_id: int
    student_username: str
    answers: List[StudentAnswer]
    score: int
    total_questions: int
    percentage: float
    completed_at: datetime
    skipped_count: int = 0  # Добавляем поле skipped_count в dataclass

# Хранилище данных (в продакшене используйте базу данных)
class DataStorage:
    def __init__(self):
        self.users: Dict[int, UserRole] = {}
        self.tests: Dict[str, Test] = {}
        self.test_results: List[TestResult] = []
        self.user_test_sessions: Dict[int, Dict] = {}  # user_id -> session data
        
        # Initialize SQLite database
        self.init_db()
    
    def init_db(self):
        """Инициализация базы данных SQLite"""
        self.conn = sqlite3.connect('bot_sessions.db', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row  # Для доступа к столбцам по имени
        self.cursor = self.conn.cursor()
        
        # Создаем таблицу для хранения сессий тестирования
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_test_sessions (
                user_id INTEGER PRIMARY KEY,
                session_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Создаем таблицу для хранения активных тестов пользователей
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_user_tests (
                user_id INTEGER PRIMARY KEY,
                test_id TEXT NOT NULL,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.conn.commit()
    
    def save_user_test_session(self, user_id: int, session_data: Dict):
        """Сохранение сессии тестирования пользователя в базе данных"""
        try:
            session_json = json.dumps(session_data, default=str)  # default=str для сериализации datetime
            self.cursor.execute('''
                INSERT OR REPLACE INTO user_test_sessions (user_id, session_data, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (user_id, session_json))
            self.conn.commit()
            logger.info(f"Сессия пользователя {user_id} сохранена в базе данных")
        except Exception as e:
            logger.error(f"Ошибка сохранения сессии пользователя {user_id} в базе данных: {e}")
    
    def get_user_test_session(self, user_id: int) -> Optional[Dict]:
        """Получение сессии тестирования пользователя из базы данных"""
        try:
            self.cursor.execute('''
                SELECT session_data FROM user_test_sessions WHERE user_id = ?
            ''', (user_id,))
            row = self.cursor.fetchone()
            if row:
                session_data = json.loads(row['session_data'])
                logger.info(f"Сессия пользователя {user_id} загружена из базы данных")
                return session_data
            else:
                logger.info(f"Сессия пользователя {user_id} не найдена в базе данных")
                return None
        except Exception as e:
            logger.error(f"Ошибка загрузки сессии пользователя {user_id} из базы данных: {e}")
            return None
    
    def delete_user_test_session(self, user_id: int):
        """Удаление сессии тестирования пользователя из базы данных"""
        try:
            self.cursor.execute('''
                DELETE FROM user_test_sessions WHERE user_id = ?
            ''', (user_id,))
            self.cursor.execute('''
                DELETE FROM active_user_tests WHERE user_id = ?
            ''', (user_id,))
            self.conn.commit()
            logger.info(f"Сессия пользователя {user_id} удалена из базы данных")
        except Exception as e:
            logger.error(f"Ошибка удаления сессии пользователя {user_id} из базы данных: {e}")
    
    def set_active_user_test(self, user_id: int, test_id: str):
        """Установка активного теста для пользователя"""
        try:
            self.cursor.execute('''
                INSERT OR REPLACE INTO active_user_tests (user_id, test_id, started_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (user_id, test_id))
            self.conn.commit()
            logger.info(f"Активный тест {test_id} установлен для пользователя {user_id}")
        except Exception as e:
            logger.error(f"Ошибка установки активного теста {test_id} для пользователя {user_id}: {e}")
    
    def get_active_user_test(self, user_id: int) -> Optional[str]:
        """Получение активного теста для пользователя"""
        try:
            self.cursor.execute('''
                SELECT test_id FROM active_user_tests WHERE user_id = ?
            ''', (user_id,))
            row = self.cursor.fetchone()
            if row:
                test_id = row['test_id']
                logger.info(f"Активный тест {test_id} загружен для пользователя {user_id}")
                return test_id
            else:
                logger.info(f"Активный тест не найден для пользователя {user_id}")
                return None
        except Exception as e:
            logger.error(f"Ошибка загрузки активного теста для пользователя {user_id}: {e}")
            return None
    
    def clear_active_user_test(self, user_id: int):
        """Очистка активного теста для пользователя"""
        try:
            self.cursor.execute('''
                DELETE FROM active_user_tests WHERE user_id = ?
            ''', (user_id,))
            self.conn.commit()
            logger.info(f"Активный тест очищен для пользователя {user_id}")
        except Exception as e:
            logger.error(f"Ошибка очистки активного теста для пользователя {user_id}: {e}")

storage = DataStorage()

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# Вспомогательные функции
def generate_test_link(test_id: str) -> str:
    """Генерирует ссылку для прохождения теста"""
    return f"https://t.me/SynapSnap_bot?start=test_{test_id}"

def generate_qr_code(test_id: str) -> BufferedInputFile:
    """Генерирует QR-код для теста"""
    link = generate_test_link(test_id)
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(link)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Конвертируем в bytes
    img_bytes = io.BytesIO()
    img.save(img_bytes, 'PNG')
    img_bytes.seek(0)
    
    return BufferedInputFile(img_bytes.read(), filename=f"test_{test_id}_qr.png")

async def safe_edit_message_text(message: types.Message, text: str, **kwargs):
    """Безопасное редактирование текста сообщения с обработкой ошибок"""
    try:
        return await message.edit_text(text, **kwargs)
    except Exception as e:
        if "message is not modified" in str(e):
            # Если сообщение не изменилось, просто возвращаем сообщение
            return message
        else:
            # Для других ошибок пробрасываем исключение
            raise

async def safe_answer_message(message: types.Message, text: str, **kwargs):
    """Безопасная отправка ответа на сообщение"""
    try:
        return await message.answer(text, **kwargs)
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения: {e}")
        raise

# Клавиатуры
def get_role_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора роли"""
    keyboard = [
        [InlineKeyboardButton(text="👨‍🏫 Учитель", callback_data="role_teacher")],
        [InlineKeyboardButton(text="👨‍🎓 Ученик", callback_data="role_student")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_teacher_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню учителя"""
    keyboard = [
        [InlineKeyboardButton(text="📝 Создать тест", callback_data="create_test")],
        [InlineKeyboardButton(text="📊 Мои тесты", callback_data="my_tests")],
        [InlineKeyboardButton(text="📈 Результаты", callback_data="test_results")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_question_type_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора типа вопроса"""
    keyboard = [
        [InlineKeyboardButton(text="📋 Варианты ответов", callback_data="type_multiple")],
        [InlineKeyboardButton(text="✏️ Ввод текста", callback_data="type_text")],
        [InlineKeyboardButton(text="📸 Фото с вариантами", callback_data="type_photo_multiple")],
        [InlineKeyboardButton(text="🖼️ Фото с вводом текста", callback_data="type_photo_text")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_continue_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура продолжения создания теста"""
    keyboard = [
        [InlineKeyboardButton(text="➕ Добавить вопрос", callback_data="add_question")],
        [InlineKeyboardButton(text="✅ Завершить тест", callback_data="finish_test")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_answer_keyboard(options: List[str]) -> InlineKeyboardMarkup:
    """Клавиатура с вариантами ответов"""
    keyboard = []
    for i, option in enumerate(options):
        keyboard.append([InlineKeyboardButton(text=f"{chr(65+i)}. {option}", callback_data=f"answer_{i}")])
    keyboard.append([InlineKeyboardButton(text="⏭️ Пропустить", callback_data="skip_question")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_skip_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для пропуска вопроса с текстовым вводом"""
    keyboard = [[InlineKeyboardButton(text="⏭️ Пропустить", callback_data="skip_question")]]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# Обработчики команд
@router.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    """Обработка команды /start"""
    user_id = message.from_user.id
    
    # Проверяем, есть ли параметр для прохождения теста
    if message.text and len(message.text.split()) > 1:
        param = message.text.split()[1]
        if param.startswith("test_"):
            test_id = param[5:]  # Убираем "test_"
            if test_id in storage.tests:
                # Переводим пользователя к прохождению теста
                storage.users[user_id] = UserRole.STUDENT
                await start_test(message, test_id, state)  # Эта функция определена ниже
                return
            else:
                await message.answer("❌ Тест не найден или больше не активен.")
    
    # Обычный старт
    if user_id not in storage.users:
        # Первое сообщение - спрашиваем роль
        await message.answer(
            "👋 Добро пожаловать в бот для создания и прохождения тестов!\n\n"
            "👉 Выберите вашу роль:",
            reply_markup=get_role_keyboard()
        )
        # Второе сообщение - информация о помощи
        await message.answer(
            "ℹ️ В боте есть команда /help, где вы можете найти всю информацию о доступных функциях.\n\n"
            "После выбора роли вы сможете использовать все возможности бота."
        )
    else:
        # Пользователь уже выбрал роль
        role = storage.users[user_id]
        if role == UserRole.TEACHER:
            await message.answer(
                "👨‍🏫 Добро пожаловать, учитель!\n\n"
                "Выберите действие:",
                reply_markup=get_teacher_menu_keyboard()
            )
        else:
            await message.answer(
                "👨‍🎓 Добро пожаловать, ученик!\n\n"
                "Для прохождения теста отсканируйте QR-код или перейдите по ссылке от учителя."
            )

@router.message(Command("help"))
async def help_handler(message: types.Message, state: FSMContext):
    """Обработка команды /help"""
    user_id = message.from_user.id
    
    # Если пользователь еще не выбрал роль
    if user_id not in storage.users:
        await message.answer(
            "ℹ️ Помощь по боту\n\n"
            "Для начала работы с ботом используйте команду /start"
        )
        return
    
    role = storage.users[user_id]
    
    if role == UserRole.TEACHER:
        # Создаем клавиатуру помощи для учителя
        keyboard = [
            [InlineKeyboardButton(text="📝 Создать тест", callback_data="create_test")],
            [InlineKeyboardButton(text="📚 Мои тесты", callback_data="my_tests")],
            [InlineKeyboardButton(text="📊 Результаты", callback_data="test_results")]
        ]
        keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")])
        
        await message.answer(
            "ℹ️ Помощь для учителя\n\n"
            "Вы можете использовать следующие функции:\n\n"
            "• 📝 Создать тест - создание нового теста\n"
            "• 📚 Мои тесты - просмотр созданных тестов\n"
            "• 📊 Результаты - просмотр результатов по всем тестам\n\n"
            "В разделе 'Мои тесты' вы также можете:\n"
            "• Просматривать результаты по конкретному тесту\n"
            "• Удалять ненужные тесты",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
    else:
        await message.answer(
            "ℹ️ Помощь для ученика\n\n"
            "Для прохождения теста отсканируйте QR-код или перейдите по ссылке от учителя.\n\n"
            "После прохождения теста ваши результаты будут отправлены учителю."
        )

@router.callback_query(F.data == "type_text")
async def choose_text_input(callback: types.CallbackQuery, state: FSMContext):
    """Выбор типа 'текстовый ввод'"""
    await safe_edit_message_text(
        callback.message,
        "✏️ Тип вопроса: Ввод текста\n\n"
        "Введите текст вопроса:"
    )
    await state.update_data(question_type=QuestionType.TEXT_INPUT)
    await state.set_state(TeacherStates.waiting_question_text)

@router.callback_query(F.data == "type_multiple")
async def choose_multiple_choice(callback: types.CallbackQuery, state: FSMContext):
    """Выбор типа 'множественный выбор'"""
    await safe_edit_message_text(
        callback.message,
        "📋 Тип вопроса: Варианты ответов\n\n"
        "Введите текст вопроса:"
    )
    await state.update_data(question_type=QuestionType.MULTIPLE_CHOICE)
    await state.set_state(TeacherStates.waiting_question_text)

@router.callback_query(F.data == "type_photo_multiple")
async def choose_photo_multiple_choice(callback: types.CallbackQuery, state: FSMContext):
    """Выбор типа 'фото с вариантами ответов'"""
    await safe_edit_message_text(
        callback.message,
        "📸 Тип вопроса: Фото с вариантами ответов\n\n"
        "Пожалуйста, отправьте фото с заданием:"
    )
    await state.update_data(question_type=QuestionType.MULTIPLE_CHOICE)
    await state.set_state(TeacherStates.waiting_photo)

@router.callback_query(F.data == "type_photo_text")
async def choose_photo_text_input(callback: types.CallbackQuery, state: FSMContext):
    """Выбор типа 'фото с вводом текста'"""
    await safe_edit_message_text(
        callback.message,
        "🖼️ Тип вопроса: Фото с вводом текста\n\n"
        "Пожалуйста, отправьте фото с заданием:"
    )
    await state.update_data(question_type=QuestionType.TEXT_INPUT)
    await state.set_state(TeacherStates.waiting_photo)

@router.callback_query(F.data == "role_teacher")
async def choose_teacher_role(callback: types.CallbackQuery):
    """Выбор роли учителя"""
    user_id = callback.from_user.id
    storage.users[user_id] = UserRole.TEACHER
    
    await safe_edit_message_text(
        callback.message,
        "👨‍🏫 Вы выбрали роль учителя!\n\n"
        "Теперь вы можете создавать тесты и отслеживать результаты учеников.\n\n"
        "Выберите действие:",
        reply_markup=get_teacher_menu_keyboard()
    )

@router.callback_query(F.data == "role_student")
async def choose_student_role(callback: types.CallbackQuery):
    """Выбор роли ученика"""
    user_id = callback.from_user.id
    storage.users[user_id] = UserRole.STUDENT
    
    await safe_edit_message_text(
        callback.message,
        "👨‍🎓 Вы выбрали роль ученика!\n\n"
        "Для прохождения теста отсканируйте QR-код или перейдите по ссылке от учителя."
    )

# Обработчики для учителя
@router.callback_query(F.data == "create_test")
async def create_test_handler(callback: types.CallbackQuery, state: FSMContext):
    """Начало создания теста"""
    await safe_edit_message_text(
        callback.message,
        "📝 Создание нового теста\n\n"
        "Введите количество вопросов в тесте:"
    )
    await state.set_state(TeacherStates.waiting_question_count)

@router.message(TeacherStates.waiting_question_count)
async def process_question_count(message: types.Message, state: FSMContext):
    """Обработка количества вопросов"""
    try:
        count = int(message.text)
        if count <= 0:
            raise ValueError()
        
        # Сохраняем количество вопросов и переходим к запросу названия теста
        await state.update_data(total_questions=count)
        
        await message.answer(
            f"✅ Тест будет содержать {count} вопрос(ов).\n\n"
            "Введите название теста (или отправьте '-', чтобы оставить без названия):"
        )
        await state.set_state(TeacherStates.creating_question)
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число больше 0:")

@router.message(TeacherStates.creating_question)
async def process_test_name(message: types.Message, state: FSMContext):
    """Обработка названия теста"""
    data = await state.get_data()
    count = data['total_questions']
    
    # Получаем название теста или оставляем пустым
    test_name = message.text if message.text != "-" else ""
    
    # Создаем новый тест
    test_id = str(uuid.uuid4())
    test = Test(
        id=test_id,
        teacher_id=message.from_user.id,
        teacher_username=message.from_user.username or str(message.from_user.id),
        questions=[],
        created_at=datetime.now(),
        name=test_name
    )
    
    await state.update_data(
        test=test,
        current_question=0
    )
    
    await message.answer(
        f"✅ Название теста: {test_name if test_name else 'Без названия'}\n\n"
        f"📝 Вопрос 1/{count}\n\n"
        "Выберите тип вопроса:",
        reply_markup=get_question_type_keyboard()
    )
    # Instead of asking for text first, we ask for question type first
    await state.set_state(TeacherStates.waiting_question_type)

@router.message(TeacherStates.waiting_question_text)
async def process_question_text(message: types.Message, state: FSMContext):
    """Обработка текста вопроса"""
    data = await state.get_data()
    current_question = data.get('current_question', 0)
    total_questions = data.get('total_questions', 0)
    question_type = data.get('question_type')
    
    await state.update_data(question_text=message.text)
    
    # Depending on the question type, we go to different states
    if question_type == QuestionType.TEXT_INPUT:
        await message.answer(
            "✏️ Тип вопроса: Ввод текста\n\n"
            "Введите правильный ответ:"
        )
        await state.set_state(TeacherStates.waiting_correct_answer)
    elif question_type == QuestionType.MULTIPLE_CHOICE:
        await message.answer(
            "📋 Тип вопроса: Варианты ответов\n\n"
            "Введите варианты ответов, каждый с новой строки:\n\n"
            "Пример:\n"
            "Москва\n"
            "Санкт-Петербург\n"
            "Казань\n"
            "Новосибирск"
        )
        await state.set_state(TeacherStates.waiting_options)

@router.message(TeacherStates.waiting_photo)
async def process_photo_question(message: types.Message, state: FSMContext):
    """Обработка фото с заданием"""
    if not message.photo:
        await message.answer("❌ Пожалуйста, отправьте фото с заданием:")
        return
    
    # Сохраняем file_id последнего (наибольшего) фото
    photo_file_id = message.photo[-1].file_id
    await state.update_data(photo_file_id=photo_file_id)
    
    # Get the question type to determine next step
    data = await state.get_data()
    question_type = data.get('question_type')
    
    await message.answer(
        "✅ Фото с заданием сохранено!\n\n"
        "Введите текст вопроса (опционально, можно отправить '-', чтобы оставить без текста):"
    )
    await state.set_state(TeacherStates.waiting_question_text_after_photo)

@router.message(TeacherStates.waiting_question_text_after_photo)
async def process_question_text_after_photo(message: types.Message, state: FSMContext):
    """Обработка текста вопроса после загрузки фото"""
    # If user sends '-', we leave the question text empty
    if message.text != "-":
        await state.update_data(question_text=message.text)
    else:
        await state.update_data(question_text="")
    
    # Get the question type to determine next step
    data = await state.get_data()
    question_type = data.get('question_type')
    
    # Depending on the question type, we go to different states
    if question_type == QuestionType.TEXT_INPUT:
        await message.answer(
            "✏️ Тип вопроса: Ввод текста\n\n"
            "Введите правильный ответ:"
        )
        await state.set_state(TeacherStates.waiting_correct_answer)
    elif question_type == QuestionType.MULTIPLE_CHOICE:
        await message.answer(
            "📋 Тип вопроса: Варианты ответов\n\n"
            "Введите варианты ответов, каждый с новой строки:\n\n"
            "Пример:\n"
            "Москва\n"
            "Санкт-Петербург\n"
            "Казань\n"
            "Новосибирск"
        )
        await state.set_state(TeacherStates.waiting_options)

@router.message(TeacherStates.waiting_options)
async def process_options(message: types.Message, state: FSMContext):
    """Обработка вариантов ответов"""
    options = [opt.strip() for opt in message.text.split('\n') if opt.strip()]
    
    if len(options) < 2:
        await message.answer("❌ Введите минимум 2 варианта ответа, каждый с новой строки:")
        return
    
    await state.update_data(options=options)
    
    # Создаем клавиатуру для выбора правильного ответа
    keyboard = []
    for i, option in enumerate(options):
        keyboard.append([InlineKeyboardButton(text=f"{chr(65+i)}. {option}", callback_data=f"correct_{i}")])
    
    await message.answer(
        "✅ Варианты ответов сохранены!\n\n"
        "Теперь выберите правильный ответ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )

@router.callback_query(F.data.startswith("correct_"))
async def process_correct_answer_choice(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора правильного ответа"""
    correct_index = int(callback.data.split('_')[1])
    data = await state.get_data()
    options = data['options']
    correct_answer = options[correct_index]
    
    await state.update_data(correct_answer=correct_answer)
    await save_question_and_continue(callback.message, state)

@router.message(TeacherStates.waiting_correct_answer)
async def process_text_answer(message: types.Message, state: FSMContext):
    """Обработка текстового ответа"""
    await state.update_data(correct_answer=message.text)
    await save_question_and_continue(message, state)

async def save_question_and_continue(message: types.Message, state: FSMContext):
    """Сохранение вопроса и продолжение создания теста"""
    data = await state.get_data()
    test = data['test']
    current_question = data['current_question']
    total_questions = data['total_questions']
    
    # Создаем вопрос
    question = Question(
        id=str(uuid.uuid4()),
        text=data.get('question_text', ''),  # Use empty string if no text
        question_type=data['question_type'],
        options=data.get('options'),
        correct_answer=data['correct_answer'],
        photo_file_id=data.get('photo_file_id')  # Добавляем photo_file_id если есть
    )
    
    test.questions.append(question)
    current_question += 1
    
    if current_question < total_questions:
        # Есть еще вопросы
        await state.update_data(
            test=test,
            current_question=current_question,
            question_text=None,
            question_type=None,
            options=None,
            correct_answer=None,
            photo_file_id=None  # Очищаем photo_file_id для следующего вопроса
        )
        
        await message.answer(
            f"✅ Вопрос {current_question}/{total_questions} сохранен!\n\n"
            f"📝 Вопрос {current_question + 1}/{total_questions}\n\n"
            "Выберите тип вопроса:",
            reply_markup=get_question_type_keyboard()
        )
        # Instead of asking for text first, we ask for question type first
        await state.set_state(TeacherStates.waiting_question_type)
    else:
        # Все вопросы созданы
        storage.tests[test.id] = test
        
        # Генерируем QR-код
        qr_code = generate_qr_code(test.id)
        link = generate_test_link(test.id)
        
        await message.answer_photo(
            qr_code,
            caption=f"🎉 Тест создан успешно!\n\n"
                   f"📋 Количество вопросов: {total_questions}\n"
                   f"🆔 ID теста: {test.id}\n\n"
                   f"📱 Ученики могут:\n"
                   f"• Отсканировать QR-код\n"
                   f"• Перейти по ссылке: {link}\n\n"
                   f"✅ Тест готов к использованию!"
        )
        
        await message.answer(
            "Что хотите делать дальше?",
            reply_markup=get_teacher_menu_keyboard()
        )
        
        await state.clear()

@router.callback_query(F.data == "my_tests")
async def show_my_tests(callback: types.CallbackQuery):
    """Показать тесты учителя"""
    user_id = callback.from_user.id
    user_tests = [test for test in storage.tests.values() if test.teacher_id == user_id]
    
    if not user_tests:
        await safe_edit_message_text(
            callback.message,
            "📝 У вас пока нет созданных тестов.\n\n"
            "Создайте первый тест!",
            reply_markup=get_teacher_menu_keyboard()
        )
        return
    
    # Создаем клавиатуру с тестами и кнопками удаления (в одной строке)
    keyboard = []
    for i, test in enumerate(user_tests, 1):
        test_title = f"«{test.name}»" if test.name else f"ID: {test.id}"
        # Добавляем кнопки в одной строке
        keyboard.append([
            InlineKeyboardButton(text=f"📝 {test_title}", callback_data=f"view_test_{test.id}"),
            InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"delete_test_{test.id}")
        ])
    
    keyboard.append([InlineKeyboardButton(text="📊 Все результаты", callback_data="test_results")])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")])
    
    text = "📚 Ваши тесты:\n\n"
    for i, test in enumerate(user_tests, 1):
        test_title = f"«{test.name}»" if test.name else f"ID: {test.id}"
        text += f"{i}. {test_title}\n"
        text += f"   Вопросов: {len(test.questions)}\n"
        text += f"   Создан: {test.created_at.strftime('%d.%m.%Y %H:%M')}\n\n"
    
    await safe_edit_message_text(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data.startswith("delete_test_"))
async def delete_test(callback: types.CallbackQuery):
    """Удаление теста"""
    user_id = callback.from_user.id
    test_id = callback.data[len("delete_test_"):]
    
    # Проверяем, что тест существует и принадлежит пользователю
    test = storage.tests.get(test_id)
    if not test:
        await callback.answer("❌ Тест не найден")
        return
    
    if test.teacher_id != user_id:
        await callback.answer("❌ У вас нет прав на удаление этого теста")
        return
    
    # Удаляем тест
    del storage.tests[test_id]
    
    # Также удаляем все результаты по этому тесту
    storage.test_results = [result for result in storage.test_results if result.test_id != test_id]
    
    await callback.answer("✅ Тест успешно удален")
    
    # Обновляем список тестов
    await show_my_tests(callback)

@router.callback_query(F.data == "test_results")
async def show_test_results(callback: types.CallbackQuery):
    """Показать результаты тестов"""
    user_id = callback.from_user.id
    user_tests = [test.id for test in storage.tests.values() if test.teacher_id == user_id]
    user_results = [result for result in storage.test_results if result.test_id in user_tests]
    
    if not user_results:
        await safe_edit_message_text(
            callback.message,
            "📊 Пока нет результатов по вашим тестам.\n\n"
            "Когда ученики пройдут тесты, здесь появится статистика.",
            reply_markup=get_teacher_menu_keyboard()
        )
        return
    
    # Группируем результаты по тестам
    results_by_test = {}
    for result in user_results:
        if result.test_id not in results_by_test:
            results_by_test[result.test_id] = []
        results_by_test[result.test_id].append(result)
    
    text = "📊 Результаты ваших тестов:\n\n"
    for test_id, results in results_by_test.items():
        test = storage.tests[test_id]
        test_title = f"«{test.name}»" if test.name else f"ID: {test_id[:8]}..."
        text += f"📝 Тест {test_title}\n"
        text += f"👥 Прошли: {len(results)} чел.\n"
        avg_score = sum(r.percentage for r in results) / len(results)
        text += f"📈 Средний балл: {avg_score:.1f}%\n\n"
        
        for result in results[-5:]:  # Показываем последние 5 результатов
            # Создаем ссылку на профиль ученика
            if result.student_username and result.student_username != str(result.student_id):
                # Если есть username, используем его
                student_link = f"@{result.student_username}"
            else:
                # Если нет username, показываем ID
                student_link = f"ID: {result.student_id}"
            
            text += f"  👤 {student_link}\n"
            text += f"  📊 {result.score}/{result.total_questions} ({result.percentage:.1f}%)\n"
            # Используем getattr для безопасного доступа к skipped_count
            skipped_count = getattr(result, 'skipped_count', 0)
            text += f"  ⏭️ Пропущено: {skipped_count}\n"
            text += f"  🕐 Завершен: {result.completed_at.strftime('%d.%m.%Y %H:%M')}\n\n"
    
    await safe_edit_message_text(callback.message, text[:4096], reply_markup=get_teacher_menu_keyboard())

@router.callback_query(F.data.startswith("view_test_"))
async def view_test_results(callback: types.CallbackQuery):
    """Просмотр результатов конкретного теста"""
    user_id = callback.from_user.id
    test_id = callback.data[len("view_test_"):]
    
    # Проверяем, что тест существует и принадлежит пользователю
    test = storage.tests.get(test_id)
    if not test:
        await callback.answer("❌ Тест не найден")
        return
    
    if test.teacher_id != user_id:
        await callback.answer("❌ У вас нет прав на просмотр результатов этого теста")
        return
    
    # Получаем результаты только для этого теста
    test_results = [result for result in storage.test_results if result.test_id == test_id]
    
    if not test_results:
        await safe_edit_message_text(
            callback.message,
            f"📊 Результаты теста «{test.name if test.name else test_id[:8]}...»\n\n"
            "Пока нет результатов по этому тесту.\n\n"
            "Когда ученики пройдут тест, здесь появится статистика.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="my_tests")]])
        )
        return
    
    # Сортируем результаты по дате завершения (новые первыми)
    test_results.sort(key=lambda r: r.completed_at, reverse=True)
    
    test_title = f"«{test.name}»" if test.name else f"ID: {test_id[:8]}..."
    text = f"📊 Результаты теста {test_title}\n\n"
    
    # Общая статистика
    avg_score = sum(r.percentage for r in test_results) / len(test_results)
    text += f"👥 Всего прошли: {len(test_results)} чел.\n"
    text += f"📈 Средний балл: {avg_score:.1f}%\n\n"
    
    # Детализированные результаты
    text += "Подробные результаты:\n\n"
    
    for result in test_results[:10]:  # Показываем последние 10 результатов
        # Создаем ссылку на профиль ученика
        if result.student_username and result.student_username != str(result.student_id):
            # Если есть username, используем его
            student_link = f"@{result.student_username}"
        else:
            # Если нет username, показываем ID
            student_link = f"ID: {result.student_id}"
        
        text += f"👤 {student_link}\n"
        text += f"📊 {result.score}/{result.total_questions} ({result.percentage:.1f}%)\n"
        # Используем getattr для безопасного доступа к skipped_count
        skipped_count = getattr(result, 'skipped_count', 0)
        text += f"⏭️ Пропущено: {skipped_count}\n"
        text += f"🕐 Завершен: {result.completed_at.strftime('%d.%m.%Y %H:%M')}\n\n"
    
    # Кнопка для возврата к списку тестов
    keyboard = [[InlineKeyboardButton(text="⬅️ Назад", callback_data="my_tests")]]
    
    await safe_edit_message_text(callback.message, text[:4096], reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

# Обработчик возврата в главное меню
@router.callback_query(F.data == "main_menu")
async def back_to_main_menu(callback: types.CallbackQuery):
    """Возврат в главное меню"""
    user_id = callback.from_user.id
    role = storage.users.get(user_id)
    
    if role == UserRole.TEACHER:
        await safe_edit_message_text(
            callback.message,
            "👨‍🏫 Главное меню учителя\n\nВыберите действие:",
            reply_markup=get_teacher_menu_keyboard()
        )
    else:
        await safe_edit_message_text(
            callback.message,
            "👨‍🎓 Для прохождения теста отсканируйте QR-код или перейдите по ссылке от учителя."
        )

# Обработчики для ученика
async def start_test(message: types.Message, test_id: str, state: FSMContext):
    """Начало прохождения теста"""
    test = storage.tests.get(test_id)
    if not test or not test.active:
        await message.answer("❌ Тест не найден или больше не активен.")
        return
    
    user_id = message.from_user.id
    username = message.from_user.username or str(user_id)
    
    # Инициализируем сессию прохождения теста
    session = {
        'test_id': test_id,
        'current_question': 0,
        'answers': [],
        'start_time': datetime.now().isoformat(),  # Сохраняем как строку для сериализации
        'student_username': username,  # Сохраняем username студента
        'student_id': user_id  # Сохраняем ID студента
    }
    
    # Сохраняем сессию в базе данных
    storage.save_user_test_session(user_id, session)
    # Устанавливаем активный тест для пользователя
    storage.set_active_user_test(user_id, test_id)
    
    await message.answer(
        f"📚 Тест готов к прохождению!\n\n"
        f"📝 Количество вопросов: {len(test.questions)}\n"
        f"👨‍🏫 Автор: @{test.teacher_username}\n\n"
        f"ℹ️ Вы можете пропускать вопросы кнопкой 'Пропустить'\n\n"
        f"Готовы начать?"
    )
    
    await show_current_question(message, state, user_id)

async def show_current_question(message: types.Message, state: FSMContext, user_id: Optional[int] = None):
    """Показать текущий вопрос"""
    # If user_id is not provided, try to get it from the message
    if user_id is None:
        if hasattr(message, 'from_user') and message.from_user:
            user_id = message.from_user.id
        else:
            # If this is called from a callback query context, we need to handle it differently
            # This is a fallback - in practice, we should pass user_id explicitly
            await message.answer("❌ Ошибка получения данных пользователя.")
            return
    
    # Debug: Log the user_id and check if session exists
    logger.info(f"show_current_question called for user_id: {user_id}")
    
    # Получаем сессию из базы данных
    if user_id is not None:
        session = storage.get_user_test_session(user_id)
    else:
        await message.answer("❌ Ошибка получения ID пользователя.")
        return
    
    if not session:
        await message.answer("❌ Сессия тестирования не найдена.")
        logger.error(f"Session not found for user_id: {user_id}")
        return
    
    test = storage.tests[session['test_id']]
    current_question_index = session['current_question']
    
    if current_question_index >= len(test.questions):
        await finish_test(message, state, user_id)
        return
    
    question = test.questions[current_question_index]
    
    # Проверяем, есть ли фото в вопросе
    if question.photo_file_id:
        # Отправляем фото с текстом вопроса
        caption = f"❓ Вопрос {current_question_index + 1}/{len(test.questions)}\n\n{question.text}"
        
        if question.question_type == QuestionType.MULTIPLE_CHOICE:
            await message.answer_photo(
                photo=question.photo_file_id,
                caption=caption,
                reply_markup=get_answer_keyboard(question.options)
            )
        else:
            await message.answer_photo(
                photo=question.photo_file_id,
                caption=f"{caption}\n\n✏️ Введите ваш ответ:",
                reply_markup=get_skip_keyboard()
            )
            await state.set_state(StudentStates.answering_question)
    else:
        # Обычный текстовый вопрос
        text = f"❓ Вопрос {current_question_index + 1}/{len(test.questions)}\n\n{question.text}"
        
        if question.question_type == QuestionType.MULTIPLE_CHOICE:
            await message.answer(
                text,
                reply_markup=get_answer_keyboard(question.options)
            )
        else:
            await message.answer(
                f"{text}\n\n✏️ Введите ваш ответ:",
                reply_markup=get_skip_keyboard()
            )
            await state.set_state(StudentStates.answering_question)

@router.callback_query(F.data.startswith("answer_"))
async def process_multiple_choice_answer(callback: types.CallbackQuery, state: FSMContext):
    """Обработка ответа с множественным выбором"""
    user_id = callback.from_user.id
    logger.info(f"process_multiple_choice_answer called for user_id: {user_id}")
    
    # Получаем сессию из базы данных
    session = storage.get_user_test_session(user_id)
    
    if not session:
        await callback.answer("❌ Сессия не найдена")
        logger.error(f"Session not found for user_id: {user_id}")
        return
    
    test = storage.tests[session['test_id']]
    question = test.questions[session['current_question']]
    
    answer_index = int(callback.data.split('_')[1])
    selected_answer = question.options[answer_index]
    is_correct = selected_answer == question.correct_answer
    
    # Сохраняем ответ
    student_answer = StudentAnswer(
        question_id=question.id,
        answer=selected_answer,
        is_correct=is_correct,
        skipped=False
    )
    
    session['answers'].append(asdict(student_answer))  # Сохраняем как словарь для сериализации
    session['current_question'] += 1
    
    # Сохраняем обновленную сессию в базе данных
    storage.save_user_test_session(user_id, session)
    
    # Показываем результат ответа
    result_text = "✅ Правильно!" if is_correct else f"❌ Неправильно! Правильный ответ: {question.correct_answer}"
    
    # Отправляем результат отдельным сообщением, чтобы не было проблем с редактированием фото
    await callback.message.answer(
        f"❓ Вопрос {session['current_question']}/{len(test.questions)}\n\n"
        f"{question.text}\n\n"
        f"Ваш ответ: {selected_answer}\n"
        f"{result_text}"
    )
    
    # Удаляем инлайн-кнопки
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass  # Игнорируем ошибки, если не удалось удалить кнопки
    
    # Пауза перед следующим вопросом
    await asyncio.sleep(1.5)
    # Pass the callback message and user_id to show_current_question
    await show_current_question(callback.message, state, user_id)

@router.message(StudentStates.answering_question)
async def process_text_answer_from_student(message: types.Message, state: FSMContext):
    """Обработка текстового ответа от ученика"""
    user_id = message.from_user.id
    logger.info(f"process_text_answer_from_student called for user_id: {user_id}")
    
    # Получаем сессию из базы данных
    session = storage.get_user_test_session(user_id)
    
    if not session:
        await message.answer("❌ Сессия не найдена")
        logger.error(f"Session not found for user_id: {user_id}")
        return
    
    test = storage.tests[session['test_id']]
    question = test.questions[session['current_question']]
    
    user_answer = message.text.strip()
    is_correct = user_answer.lower() == question.correct_answer.lower()
    
    # Сохраняем ответ
    student_answer = StudentAnswer(
        question_id=question.id,
        answer=user_answer,
        is_correct=is_correct,
        skipped=False
    )
    
    session['answers'].append(asdict(student_answer))  # Сохраняем как словарь для сериализации
    session['current_question'] += 1
    
    # Сохраняем обновленную сессию в базе данных
    storage.save_user_test_session(user_id, session)
    
    # Показываем результат
    result_text = "✅ Правильно!" if is_correct else f"❌ Неправильно! Правильный ответ: {question.correct_answer}"
    
    await message.answer(
        f"Ваш ответ: {user_answer}\n{result_text}"
    )
    
    await state.clear()
    await asyncio.sleep(1.5)
    await show_current_question(message, state, user_id)

@router.callback_query(F.data == "skip_question")
async def skip_question(callback: types.CallbackQuery, state: FSMContext):
    """Пропуск вопроса"""
    user_id = callback.from_user.id
    logger.info(f"skip_question called for user_id: {user_id}")
    
    # Получаем сессию из базы данных
    session = storage.get_user_test_session(user_id)
    
    if not session:
        await callback.answer("❌ Сессия не найдена")
        logger.error(f"Session not found for user_id: {user_id}")
        return
    
    test = storage.tests[session['test_id']]
    question = test.questions[session['current_question']]
    
    # Сохраняем пропуск
    student_answer = StudentAnswer(
        question_id=question.id,
        answer="",
        is_correct=False,
        skipped=True
    )
    
    session['answers'].append(asdict(student_answer))  # Сохраняем как словарь для сериализации
    session['current_question'] += 1
    
    # Сохраняем обновленную сессию в базе данных
    storage.save_user_test_session(user_id, session)
    
    # Отправляем сообщение об пропуске отдельно, чтобы не было проблем с редактированием фото
    await callback.message.answer("⏭️ Вопрос пропущен")
    
    # Удаляем инлайн-кнопки
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass  # Игнорируем ошибки, если не удалось удалить кнопки
    
    await state.clear()
    await asyncio.sleep(1)
    
    # Проверяем, есть ли еще вопросы  
    if session['current_question'] >= len(test.questions):
        # Создаем фиктивное сообщение с правильным пользователем
        await finish_test(callback.message, state, user_id)
    else:
        # Pass the callback message and user_id to show_current_question
        await show_current_question(callback.message, state, user_id)

async def finish_test(message: types.Message, state: FSMContext, user_id: Optional[int] = None):
    """Завершение теста"""
    # Получаем сессию из базы данных
    if user_id is not None:
        session = storage.get_user_test_session(user_id)
    else:
        # Если user_id не передан, пытаемся получить его из сообщения
        if hasattr(message, 'from_user') and message.from_user:
            user_id = message.from_user.id
            session = storage.get_user_test_session(user_id)
        else:
            await message.answer("❌ Ошибка получения данных пользователя.")
            return
    
    if not session:
        await message.answer("❌ Сессия не найдена")
        return
    
    # Проверка, что user_id определен
    if user_id is None:
        user_id = session.get('student_id', message.from_user.id if hasattr(message, 'from_user') and message.from_user else 0)
        if user_id == 0:
            await message.answer("❌ Ошибка получения ID пользователя.")
            return
    
    # Получаем username из сессии
    username = session.get('student_username', str(user_id))
    
    test = storage.tests[session['test_id']]
    
    # Преобразуем ответы из словарей обратно в объекты StudentAnswer
    answers = []
    for answer_dict in session['answers']:
        answer = StudentAnswer(
            question_id=answer_dict['question_id'],
            answer=answer_dict['answer'],
            is_correct=answer_dict['is_correct'],
            skipped=answer_dict.get('skipped', False)
        )
        answers.append(answer)
    
    # Подсчитываем результаты
    correct_count = sum(1 for answer in answers if answer.is_correct)
    total_questions = len(test.questions)
    skipped_count = sum(1 for answer in answers if answer.skipped)
    percentage = (correct_count / total_questions) * 100
    
    # Создаем результат
    result = TestResult(
        test_id=test.id,
        student_id=user_id,
        student_username=username,
        answers=answers,
        score=correct_count,
        total_questions=total_questions,
        percentage=percentage,
        completed_at=datetime.now(),
        skipped_count=skipped_count  # Устанавливаем skipped_count при создании
    )
    
    storage.test_results.append(result)
    
    # Показываем результат ученику
    result_text = f"🎉 Тест завершен!\n\n"
    result_text += f"📊 Ваши результаты:\n"
    result_text += f"✅ Правильных ответов: {correct_count}/{total_questions}\n"
    result_text += f"📈 Процент правильных: {percentage:.1f}%\n"
    result_text += f"⏭️ Пропущено вопросов: {skipped_count}\n\n"
    
    if skipped_count > 0:
        skipped_questions = []
        for i, answer in enumerate(answers):
            if answer.skipped:
                skipped_questions.append(f"  • Вопрос {i+1}")
        result_text += f"❓ Пропущенные вопросы:\n" + "\n".join(skipped_questions) + "\n\n"
    
    # Определяем оценку
    if percentage >= 90:
        result_text += "🌟 Отлично! Превосходный результат!"
    elif percentage >= 75:
        result_text += "👍 Хорошо! Неплохие знания!"
    elif percentage >= 60:
        result_text += "👌 Удовлетворительно. Есть над чем поработать."
    else:
        result_text += "📚 Стоит повторить материал."
    
    await message.answer(result_text)
    
    # Уведомляем учителя о новом результате
    teacher = test.teacher_id
    try:
        test_title = f"«{test.name}»" if test.name else f"ID: {test.id[:8]}..."
        await bot.send_message(
            teacher,
            f"📋 Новый результат теста!\n\n"
            f"👤 Ученик: @{username}\n"
            f"📝 Тест {test_title}\n"
            f"📊 Результат: {correct_count}/{total_questions} ({percentage:.1f}%)\n"
            f"⏭️ Пропущено: {skipped_count}\n"
            f"⏰ Завершен: {result.completed_at.strftime('%d.%m.%Y %H:%M')}\n\n"
            f"Подробная статистика доступна в разделе 'Результаты'."
        )
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление учителю {teacher}: {e}")
    
    # Очищаем сессию из базы данных
    if user_id is not None:
        storage.delete_user_test_session(user_id)
        storage.clear_active_user_test(user_id)
    await state.clear()

# Обработчик неизвестных callback'ов
@router.callback_query()
async def unknown_callback(callback: types.CallbackQuery):
    """Обработка неизвестных callback'ов"""
    await callback.answer("❌ Неизвестная команда")

# Обработчик всех остальных сообщений
@router.message()
async def unknown_message(message: types.Message, state: FSMContext):
    """Обработка неизвестных сообщений"""
    user_id = message.from_user.id
    current_state = await state.get_state()
    
    # Если пользователь находится в процессе создания теста или прохождения - игнорируем
    if current_state:
        return
    
    role = storage.users.get(user_id)
    
    if role == UserRole.TEACHER:
        await message.answer(
            "👨‍🏫 Используйте меню для навигации:",
            reply_markup=get_teacher_menu_keyboard()
        )
    elif role == UserRole.STUDENT:
        await message.answer(
            "👨‍🎓 Для прохождения теста используйте QR-код или ссылку от учителя.\n\n"
            "Или напишите /start для начала работы с ботом."
        )
    else:
        await message.answer(
            "👋 Добро пожаловать! Используйте /start для начала работы.",
            reply_markup=get_role_keyboard()
        )

# Функции для сохранения и загрузки данных
async def save_data():
    """Сохранение данных в файл"""
    try:
        # Преобразуем данные в сериализуемый формат
        data = {
            'users': {str(k): v.value for k, v in storage.users.items()},
            'tests': {},
            'test_results': []
        }
        
        # Сериализуем тесты
        for test_id, test in storage.tests.items():
            test_data = asdict(test)
            # Конвертируем datetime в строку
            test_data['created_at'] = test.created_at.isoformat()
            # Конвертируем QuestionType в строку
            for question in test_data['questions']:
                question['question_type'] = question['question_type'].value
            data['tests'][test_id] = test_data
        
        # Сериализуем результаты
        for result in storage.test_results:
            result_data = asdict(result)
            result_data['completed_at'] = result.completed_at.isoformat()
            # Преобразуем StudentAnswer объекты в словари
            answers_data = []
            for answer in result_data['answers']:
                if isinstance(answer, StudentAnswer):
                    answers_data.append(asdict(answer))
                else:
                    answers_data.append(answer)
            result_data['answers'] = answers_data
            data['test_results'].append(result_data)
        
        # Сохраняем в файл
        with open('bot_data.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            
        logger.info("Данные сохранены")
    except Exception as e:
        logger.error(f"Ошибка сохранения данных: {e}")

async def load_data():
    """Загрузка данных из файла"""
    try:
        with open('bot_data.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Восстанавливаем пользователей
        for user_id, role in data.get('users', {}).items():
            storage.users[int(user_id)] = UserRole(role)
        
        # Восстанавливаем тесты
        for test_id, test_data in data.get('tests', {}).items():
            questions = []
            for q_data in test_data['questions']:
                question = Question(
                    id=q_data['id'],
                    text=q_data['text'],
                    question_type=QuestionType(q_data['question_type']),
                    options=q_data.get('options'),
                    correct_answer=q_data['correct_answer']
                )
                questions.append(question)
            
            test = Test(
                id=test_data['id'],
                teacher_id=test_data['teacher_id'],
                teacher_username=test_data['teacher_username'],
                questions=questions,
                created_at=datetime.fromisoformat(test_data['created_at']),
                name=test_data.get('name', ''),
                active=test_data.get('active', True)
            )
            storage.tests[test_id] = test
        
        # Восстанавливаем результаты
        for result_data in data.get('test_results', []):
            answers = []
            for a_data in result_data['answers']:
                # a_data может быть либо словарем, либо уже StudentAnswer объектом
                if isinstance(a_data, dict):
                    answer = StudentAnswer(
                        question_id=a_data['question_id'],
                        answer=a_data['answer'],
                        is_correct=a_data['is_correct'],
                        skipped=a_data.get('skipped', False)
                    )
                else:
                    answer = a_data
                answers.append(answer)
            
            # Создаем результат с правильной инициализацией skipped_count
            result = TestResult(
                test_id=result_data['test_id'],
                student_id=result_data['student_id'],
                student_username=result_data['student_username'],
                answers=answers,
                score=result_data['score'],
                total_questions=result_data['total_questions'],
                percentage=result_data['percentage'],
                completed_at=datetime.fromisoformat(result_data['completed_at']),
                skipped_count=result_data.get('skipped_count', sum(1 for a in answers if getattr(a, 'skipped', False)))
            )
            storage.test_results.append(result)
        
        logger.info(f"Данные загружены: пользователи={len(storage.users)}, тесты={len(storage.tests)}, результаты={len(storage.test_results)}")
        
    except FileNotFoundError:
        logger.info("Файл данных не найден, начинаем с чистого листа")
    except Exception as e:
        logger.error(f"Ошибка загрузки данных: {e}")

# Функция периодического сохранения
async def periodic_save():
    """Периодическое сохранение данных каждые 5 минут"""
    while True:
        await asyncio.sleep(300)  # 5 минут
        await save_data()

# Функция graceful shutdown
async def on_shutdown():
    """Действия при завершении работы бота"""
    logger.info("Завершение работы бота...")
    await save_data()
    await bot.session.close()

# Главная функция
async def main():
    """Основная функция запуска бота"""
    logger.info("Запуск бота...")
    
    # Загружаем сохраненные данные
    await load_data()
    
    # Регистрируем роутер
    dp.include_router(router)
    
    # Запускаем фоновое сохранение данных
    save_task = asyncio.create_task(periodic_save())
    
    # Параметры повторных попыток
    retry_count = 0
    max_retries = 10
    
    while retry_count < max_retries:
        try:
            # Запускаем polling с увеличенными таймаутами и улучшенной обработкой ошибок
            await dp.start_polling(
                bot, 
                skip_updates=True,
                allowed_updates=["message", "callback_query"],  # Ограничиваем типы обновлений
                timeout=60,  # Таймаут для запросов
                request_timeout=60  # Таймаут для сетевых запросов
            )
            break  # Если успешно, выходим из цикла
        except KeyboardInterrupt:
            logger.info("Получен сигнал завершения")
            break
        except Exception as e:
            retry_count += 1
            logger.error(f"Критическая ошибка (попытка {retry_count}/{max_retries}): {e}")
            # Добавляем повторную попытку через 5 секунд при сетевых ошибках
            if "timeout" in str(e).lower() or "network" in str(e).lower():
                logger.info("Повторная попытка через 5 секунд...")
                await asyncio.sleep(5)
            else:
                # Для других ошибок ждем дольше
                logger.info("Повторная попытка через 10 секунд...")
                await asyncio.sleep(10)
    else:
        logger.error("Превышено максимальное количество попыток. Завершение работы.")
    
    # Останавливаем фоновые задачи
    save_task.cancel()
    try:
        await save_task
    except asyncio.CancelledError:
        pass
    
    # Сохраняем данные и закрываем соединения
    await on_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")

# === ДОПОЛНИТЕЛЬНЫЕ ФАЙЛЫ ДЛЯ ДЕПЛОЯ ===

# requirements.txt
REQUIREMENTS = """
aiogram==3.7.0
qrcode[pil]==7.4.2
Pillow==10.0.0
"""

# .env файл для переменных окружения
ENV_TEMPLATE = """
BOT_TOKEN=your_bot_token_here
WEBHOOK_URL=https://your-domain.com
DEBUG=False
"""

# Dockerfile
DOCKERFILE = """
FROM python:3.11-slim

WORKDIR /app

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y \\
    gcc \\
    && rm -rf /var/lib/apt/lists/*

# Копируем и устанавливаем Python зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код приложения
COPY . .

# Создаем директорию для данных
RUN mkdir -p /app/data

# Запускаем приложение
CMD ["python", "bot.py"]
"""

# docker-compose.yml
DOCKER_COMPOSE = """
version: '3.8'

services:
  telegram-bot:
    build: .
    environment:
      - BOT_TOKEN=${BOT_TOKEN}
      - WEBHOOK_URL=${WEBHOOK_URL}
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    restart: unless-stopped
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

  # Опционально - добавить PostgreSQL
  # postgres:
  #   image: postgres:15
  #   environment:
  #     POSTGRES_DB: telegram_bot
  #     POSTGRES_USER: bot_user
  #     POSTGRES_PASSWORD: your_password
  #   volumes:
  #     - postgres_data:/var/lib/postgresql/data
  #   restart: unless-stopped

# volumes:
#   postgres_data:
"""

# Инструкции по развертыванию
DEPLOYMENT_GUIDE = """
🚀 ИНСТРУКЦИЯ ПО РАЗВЕРТЫВАНИЮ БОТА

1. ПОДГОТОВКА
   ===============
   • Создайте бота через @BotFather в Telegram
   • Получите токен бота
   • Придумайте username для бота (для QR-кодов)

2. ЛОКАЛЬНЫЙ ЗАПУСК
   =================
   pip install aiogram==3.7.0 qrcode[pil] Pillow
   
   # Замените BOT_TOKEN в коде на ваш токен
   python py.py

3. DOCKER ДЕПЛОЙ
   ===============
   # Создайте файлы:
   # - requirements.txt (содержимое из REQUIREMENTS)
   # - Dockerfile (содержимое из DOCKERFILE)
   # - docker-compose.yml (содержимое из DOCKER_COMPOSE)
   # - .env (содержимое из ENV_TEMPLATE)
   
   docker-compose up -d

4. ПРОДАКШН НАСТРОЙКИ
   ===================
   • Настройте nginx как reverse proxy
   • Добавьте SSL сертификат
   • Настройте логирование
   • Подключите PostgreSQL вместо JSON файлов
   • Добавьте мониторинг (Prometheus + Grafana)

5. WEBHOOK (для продакшена)
   =========================
   # Добавьте в код:
   from aiohttp import web, web_runner
   
   async def webhook_handler(request):
       data = await request.json()
       update = types.Update(**data)
       await dp.feed_webhook_update(bot, update)
       return web.Response()
   
   app = web.Application()
   app.router.add_post("/webhook", webhook_handler)
   
   # Установите webhook:
   await bot.set_webhook("https://yourdomain.com/webhook")

📋 ФУНКЦИИ БОТА:
===============
✅ Выбор роли учитель/ученик
✅ Создание тестов с разными типами вопросов  
✅ QR-коды для доступа к тестам
✅ Прохождение тестов с пропусками
✅ Подробная статистика и результаты
✅ Уведомления учителей
✅ Сохранение данных
✅ Обработка ошибок

🔧 ГОТОВ К ИСПОЛЬЗОВАНИЮ!
"""

print("=" * 50)
print("🤖 TELEGRAM BOT ДЛЯ ТЕСТИРОВАНИЯ")
print("=" * 50)
print(f"📝 Файл создан: {__file__}")
print("🚀 Инструкции по запуску в комментариях")
print("=" * 50)