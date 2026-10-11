"""
Pydantic-модели для валидации входных/выходных данных API.
"""
from datetime import datetime
from enum import Enum
from typing import List, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field


class Priority(str, Enum):
    high = "High"
    mid = "Mid"
    low = "Low"


class Status(str, Enum):
    todo = "To Do"
    in_progress = "In Progress"
    done = "Done"


class RecurrencePattern(str, Enum):
    daily = "daily"
    weekdays = "weekdays"   # по будням (пн–пт)
    weekly = "weekly"       # раз в неделю, в тот же день
    days = "days"           # в выбранные дни недели: recurrence_days, 0 = пн … 6 = вс
    monthly = "monthly"     # того же числа: recurrence_days = [число]; 31-е в коротком месяце — последний день
    yearly = "yearly"


class TaskBase(BaseModel):
    task: str = Field(..., min_length=1, description="Описание задачи")
    # Название категории пользователя. Список у каждого свой (таблица categories),
    # допустимость проверяет сервер в task_service, а не модель.
    category: str = Field(..., min_length=1, max_length=30)
    source: Optional[str] = None
    deadline: Optional[datetime] = None
    ends_at: Optional[datetime] = None  # «до»: конец задачи в тот же день или позже, «голые часы» как deadline
    priority: Priority = Priority.mid
    status: Status = Status.todo
    note: Optional[str] = None
    location: Optional[str] = Field(None, max_length=200)  # место (адрес текстом), migration 014
    is_recurring: bool = False
    recurrence_pattern: Optional[RecurrencePattern] = None
    recurrence_days: Optional[List[int]] = None
    tags: List[str] = Field(default_factory=list)


class TaskCreate(TaskBase):
    """Тело запроса на создание задачи (POST /tasks)."""
    pass


class ParseRequest(BaseModel):
    """Тело запроса на разбор сырого текста (POST /tasks/parse)."""
    text: str = Field(..., min_length=1, description="Сырой текст с задачами")
    source: Optional[str] = Field(
        None, description="Подсказка об источнике, например название курса"
    )
    default_date: Optional[str] = Field(
        None, description="YYYY-MM-DD: день, открытый в приложении; задачи без даты в тексте встают на него"
    )


class TaskUpdate(BaseModel):
    """Тело запроса на обновление задачи (PATCH /tasks/{id}).
    Все поля опциональны — обновляются только переданные."""
    task: Optional[str] = None
    category: Optional[str] = Field(None, min_length=1, max_length=30)
    source: Optional[str] = None
    deadline: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    priority: Optional[Priority] = None
    status: Optional[Status] = None
    note: Optional[str] = None
    location: Optional[str] = Field(None, max_length=200)
    is_recurring: Optional[bool] = None
    recurrence_pattern: Optional[RecurrencePattern] = None
    recurrence_days: Optional[List[int]] = None
    tags: Optional[List[str]] = None


class Task(TaskBase):
    """Полная модель задачи, как она возвращается из API."""
    id: UUID
    google_event_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    comments_count: int = 0  # комментариев к задаче (значок 💬 в списке)
    files_count: int = 0     # из них с фото или PDF (значок 📎)
    money_recurring_id: Optional[UUID] = None  # задача повторяющегося платежа «по галочке» (migration 013)
    shared_id: Optional[UUID] = None           # совместная задача: id главной копии (migration 014)
    shared: Optional[dict] = None              # {"with": [имена], "pending": [имена]} — с кем общая, кто ещё не ответил

    class Config:
        from_attributes = True


class CommentFile(BaseModel):
    """Файл комментария в JSON: фото или PDF в base64 (до 10 МБ; проверяет comments_service)."""
    name: Optional[str] = None
    type: str
    data: str


class ShareIn(BaseModel):
    """POST /tasks/{id}/share: с кем поделиться задачей (id из GET /people)."""
    user_id: UUID


class NotePhotoIn(BaseModel):
    """POST /notes/{id}/files: фото в base64 (сжимается на телефоне до 1600 px)."""
    file: CommentFile


class CommentCreate(BaseModel):
    text: Optional[str] = None
    file: Optional[CommentFile] = None


class CategoryCreate(BaseModel):
    """Тело POST /categories. Длину названия и цвет из палитры проверяет categories_service."""
    name: str
    color: str


class CategoryUpdate(BaseModel):
    """Тело PATCH /categories/{id}: новое название и/или цвет."""
    name: Optional[str] = None
    color: Optional[str] = None


class CategoryOrder(BaseModel):
    """Тело PUT /categories/order: полный список id категорий в нужном порядке."""
    ids: List[UUID]


class NoteCreate(BaseModel):
    content: str = Field(..., min_length=1)
    location: Optional[str] = Field(None, max_length=200)


class NoteUpdate(BaseModel):
    content: Optional[str] = Field(None, min_length=1)
    location: Optional[str] = Field(None, max_length=200)  # "" или null — убрать место


class Note(BaseModel):
    id: UUID
    content: str
    created_at: datetime
    updated_at: datetime
    location: Optional[str] = None
    files: list[dict] = []  # фото заметки: {id, type, url} (url — подписанная ссылка на час)

    class Config:
        from_attributes = True


class SubtaskCreate(BaseModel):
    content: str = Field(..., min_length=1)


class SubtaskUpdate(BaseModel):
    content: Optional[str] = None
    is_done: Optional[bool] = None


class Subtask(BaseModel):
    id: UUID
    task_id: UUID
    content: str
    is_done: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Деньги (задача №3). Тип, сумму, валюту, категорию и дату проверяет money_service
# (с понятным текстом ошибки); модели только переносят значения. user_id клиент не присылает.

Amount = Union[str, int, float]  # «3,5» строкой тоже принимается


class MoneyCategoryCreate(BaseModel):
    kind: str
    name: str
    color: str


class MoneyCategoryUpdate(BaseModel):
    """Название, цвет и месячный лимит (только у расходов, null — снять); тип категории (kind) не меняется."""
    name: Optional[str] = None
    color: Optional[str] = None
    monthly_limit: Optional[Amount] = None


class MoneyCategoryOrder(BaseModel):
    kind: str
    ids: List[UUID]


class TransactionCreate(BaseModel):
    kind: str
    amount: Amount
    currency: str
    category_id: str
    spent_on: Optional[str] = None  # без даты — сегодня в поясе пользователя
    note: Optional[str] = None
    repeat: Optional[str] = None  # «Повторять»: weekly | biweekly | monthly | yearly — заодно создаётся правило
    as_task: Optional[bool] = None  # «Списывать после галочки» для правила (migration 013)


class TransactionUpdate(BaseModel):
    kind: Optional[str] = None
    amount: Optional[Amount] = None
    currency: Optional[str] = None
    category_id: Optional[str] = None
    spent_on: Optional[str] = None
    note: Optional[str] = None


class MoneyParseRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class RecurringCreate(BaseModel):
    kind: str
    amount: Amount
    currency: str
    category_id: str
    day_of_month: Optional[Union[int, str]] = None  # прежний ежемесячный платёж без даты первой операции
    frequency: Optional[str] = None                  # weekly | biweekly | monthly | yearly (по умолчанию monthly)
    anchor_date: Optional[str] = None                # дата уже записанной первой операции
    note: Optional[str] = None
    active: bool = True
    as_task: bool = False  # «Списывать после галочки»: платёж висит задачей (migration 013)


class RecurringUpdate(BaseModel):
    kind: Optional[str] = None
    amount: Optional[Amount] = None
    currency: Optional[str] = None
    category_id: Optional[str] = None
    day_of_month: Optional[Union[int, str]] = None
    frequency: Optional[str] = None
    next_run: Optional[str] = None  # дата следующего платежа, не раньше сегодня
    note: Optional[str] = None
    active: Optional[bool] = None
    as_task: Optional[bool] = None


class ApplePayIn(BaseModel):
    """Тело POST /money/applepay от автоматизации «Транзакция» в «Командах» iPhone."""
    amount: Amount                  # как его отдаёт iPhone: «€23,40», «4 000 ₸», «KZT 4,000.00»
    merchant: Optional[str] = None  # название магазина
    card: Optional[str] = None      # карта (не сохраняется)


class StatementIn(BaseModel):
    """POST /money/statement: период; send — прислать файл в бот (в Telegram так надёжнее), иначе отдать PDF."""
    date_from: str = Field(..., alias="from")
    date_to: str = Field(..., alias="to")
    send: bool = False


class VoiceIn(BaseModel):
    """POST /voice/transcribe: запись с микрофона приложения (base64), тип — как его дал MediaRecorder."""
    audio: str = Field(..., min_length=10, max_length=14_000_000)  # ~10 МБ после декодирования
    type: str = Field("audio/webm", max_length=60)


class GoalCreate(BaseModel):
    """POST /money/goals: суммы строкой, как везде в деньгах; проверяет goals_service."""
    name: str
    target: Union[str, float, int]
    currency: Optional[str] = None
    due_date: Optional[str] = None
    initial: Optional[Union[str, float, int]] = None


class GoalUpdate(BaseModel):
    name: Optional[str] = None
    target: Optional[Union[str, float, int]] = None
    currency: Optional[str] = None
    due_date: Optional[str] = None


class GoalMove(BaseModel):
    amount: Union[str, float, int]
    take: bool = False  # true — снять из цели


class CalendarChoice(BaseModel):
    """PUT /google/calendars: из каких календарей переносить события ("primary" — основной)."""
    ids: List[str] = Field(..., min_length=1, max_length=30)


class MeUpdate(BaseModel):
    default_currency: Optional[str] = None
    second_currency: Optional[str] = None   # дополнительная валюта (вторая кнопка в форме траты)
    calendar_import: Optional[bool] = None  # переносить ли события Google Calendar в задачи
    remind_before: Optional[int] = None     # за сколько минут напоминать о задаче со временем: 0, 15, 30, 60
