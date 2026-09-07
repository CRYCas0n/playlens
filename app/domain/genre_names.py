"""Russian names for the source's genre vocabulary.

The source publishes genres in English from a closed list — 57 values across the whole
catalogue at the time of writing. A closed list does not need a model: a table is free,
instant, identical every time, and reviewable by a person who knows the subject, which a
translation call is not.

The stored `Genre.name` keeps the source's own wording. It is what the slug is built
from, what similarity matches on, and what a reader sees if a new genre appears before
this table learns about it — an untranslated chip is a small blemish, and inventing a
translation at runtime for a term of art is a larger one.

Terms that Russian players use in English stay in English: FPS, RPG, JRPG, roguelike,
metroidvania. Translating those would be less comprehensible, not more.
"""

from __future__ import annotations

GENRE_RU: dict[str, str] = {
    "2D Beat-'Em-Up": "2D-избивалка",
    "2D Fighting": "2D-файтинг",
    "2D Platformer": "2D-платформер",
    "3D Beat-'Em-Up": "3D-избивалка",
    "3D Fighting": "3D-файтинг",
    "3D Platformer": "3D-платформер",
    "Action": "Экшен",
    "Action Adventure": "Экшен-приключение",
    "Action Puzzle": "Экшен-головоломка",
    "Action RPG": "Action RPG",
    "Adventure": "Приключение",
    "Auto Racing": "Автогонки",
    "Auto Racing Sim": "Автосимулятор",
    "Baseball": "Бейсбол",
    "Basketball": "Баскетбол",
    "Basketball Sim": "Симулятор баскетбола",
    "Board": "Настольная",
    "Card Battle": "Карточные бои",
    "Compilation": "Сборник",
    "Defense": "Оборона башен",
    "FPS": "FPS",
    "First-Person Adventure": "Приключение от первого лица",
    "Football Sim": "Симулятор американского футбола",
    "Horizontal Shoot-'Em-Up": "Горизонтальный шмап",
    "JRPG": "JRPG",
    "Linear Action Adventure": "Линейное экшен-приключение",
    "Management": "Менеджмент",
    "Metroidvania": "Метроидвания",
    "Open-World Action": "Экшен в открытом мире",
    "Party": "Для компании",
    "Point-and-Click": "Point-and-click",
    "RPG": "RPG",
    "Rail Shooter": "Рельсовый шутер",
    "Real-Time Strategy": "Стратегия в реальном времени",
    "Real-Time Tactics": "Тактика в реальном времени",
    "Rhythm": "Ритм-игра",
    "Roguelike": "Roguelike",
    "Sandbox": "Песочница",
    "Simulation": "Симулятор",
    "Soccer": "Футбол",
    "Soccer Sim": "Симулятор футбола",
    "Strategy": "Стратегия",
    "Survival": "Выживание",
    "Tactical FPS": "Тактический FPS",
    "Third Person Shooter": "Шутер от третьего лица",
    "Third-Person Adventure": "Приключение от третьего лица",
    "Top-Down Shoot-'Em-Up": "Вертикальный шмап",
    "Trainer RPG": "RPG с воспитанием персонажей",
    "Turn-Based Strategy": "Пошаговая стратегия",
    "Turn-Based Tactics": "Пошаговая тактика",
    "Tycoon": "Экономический симулятор",
    "Vehicle Combat Sim": "Симулятор боевой техники",
    "Vertical Shoot-'Em-Up": "Вертикальный шмап",
    "Virtual Career": "Виртуальная карьера",
    "Virtual Life": "Симулятор жизни",
    "Visual Novel": "Визуальная новелла",
    "Western RPG": "Западная RPG",
}


def genre_ru(name: str) -> str:
    """The Russian name, or the source's own if this table has not met it yet."""
    return GENRE_RU.get(name, name)
