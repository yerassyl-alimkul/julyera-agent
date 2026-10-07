"""
Pydantic-модели для валидации входных/выходных данных API.
"""
from datetime import datetime
from enum import Enum
from typing import List, Optional
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
    weekly = "weekly"
    monthly = "monthly"


class TaskBase(BaseModel):
    task: str = Field(..., min_length=1, description="Описание задачи")
    # Название категории пользователя. Список у каждого свой (таблица categories),
    # допустимость проверяет сервер в task_service, а не модель.
    category: str = Field(..., min_length=1, max_length=30)
    source: Optional[str] = None
    deadline: Optional[datetime] = None
    priority: Priority = Priority.mid
    status: Status = Status.todo
    note: Optional[str] = None
    is_recurring: bool = False
    recurrence_pattern: Optional[RecurrencePattern] = None
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


class TaskUpdate(BaseModel):
    """Тело запроса на обновление задачи (PATCH /tasks/{id}).
    Все поля опциональны — обновляются только переданные."""
    task: Optional[str] = None
    category: Optional[str] = Field(None, min_length=1, max_length=30)
    source: Optional[str] = None
    deadline: Optional[datetime] = None
    priority: Optional[Priority] = None
    status: Optional[Status] = None
    note: Optional[str] = None
    is_recurring: Optional[bool] = None
    recurrence_pattern: Optional[RecurrencePattern] = None
    tags: Optional[List[str]] = None


class Task(TaskBase):
    """Полная модель задачи, как она возвращается из API."""
    id: UUID
    google_event_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


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


class NoteUpdate(BaseModel):
    content: str = Field(..., min_length=1)


class Note(BaseModel):
    id: UUID
    content: str
    created_at: datetime
    updated_at: datetime

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
