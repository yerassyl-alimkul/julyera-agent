"""
Pydantic-модели для валидации входных/выходных данных API.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class Category(str, Enum):
    study = "Study"
    work = "Work"
    personal = "Personal"
    unime = "UniMe"


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
    category: Category
    source: Optional[str] = None
    deadline: Optional[datetime] = None
    priority: Priority = Priority.mid
    status: Status = Status.todo
    note: Optional[str] = None
    is_recurring: bool = False
    recurrence_pattern: Optional[RecurrencePattern] = None


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
    category: Optional[Category] = None
    source: Optional[str] = None
    deadline: Optional[datetime] = None
    priority: Optional[Priority] = None
    status: Optional[Status] = None
    note: Optional[str] = None
    is_recurring: Optional[bool] = None
    recurrence_pattern: Optional[RecurrencePattern] = None


class Task(TaskBase):
    """Полная модель задачи, как она возвращается из API."""
    id: UUID
    google_event_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


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
