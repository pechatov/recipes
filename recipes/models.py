import hashlib
import re
import unicodedata
from decimal import Decimal

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils.text import slugify

from .validators import validate_recipe_image


RUSSIAN_TRANSLITERATION = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }
)


def recipe_slug_base(title: str) -> str:
    """Build a stable ASCII URL component from Russian or Latin recipe titles."""
    transliterated = str(title or "").casefold().translate(RUSSIAN_TRANSLITERATION)
    ascii_title = unicodedata.normalize("NFKD", transliterated).encode(
        "ascii", "ignore"
    ).decode()
    return (slugify(ascii_title) or "recipe")[:210].rstrip("-")


def is_water_ingredient_name(name: str) -> bool:
    """Return whether an ingredient is plain water rather than a food product."""
    words = set(re.findall(r"[a-zа-я]+", (name or "").lower().replace("ё", "е")))
    water_words = {
        "вода",
        "воды",
        "воду",
        "water",
        "кипяток",
        "кипятка",
        "лед",
        "льда",
        "ice",
    }
    qualifiers = {
        "горячая",
        "горячей",
        "горячую",
        "холодная",
        "холодной",
        "холодную",
        "теплая",
        "теплой",
        "теплую",
        "питьевая",
        "питьевой",
        "питьевую",
        "фильтрованная",
        "фильтрованной",
        "фильтрованную",
        "кипяченая",
        "кипяченой",
        "кипяченую",
        "ледяная",
        "ледяной",
        "ледяную",
        "газированная",
        "газированной",
        "газированную",
        "минеральная",
        "минеральной",
        "минеральную",
        "комнатная",
        "комнатной",
        "комнатную",
        "температура",
        "температуры",
        "кубик",
        "кубики",
        "кубиках",
        "колотый",
        "колотого",
        "в",
        "из",
    }
    return bool(words & water_words) and words <= water_words | qualifiers


class Category(models.Model):
    slug = models.SlugField(max_length=60, unique=True)
    name = models.CharField("название", max_length=80, unique=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "категория"
        verbose_name_plural = "категории"

    def __str__(self):
        return self.name


class Recipe(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        PUBLISHED = "published", "Опубликован"

    title = models.CharField("название", max_length=180)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    description = models.TextField("описание", blank=True)
    servings = models.PositiveSmallIntegerField("порций", default=2)
    prep_minutes = models.PositiveSmallIntegerField("подготовка, минут", default=0)
    cook_minutes = models.PositiveSmallIntegerField("приготовление, минут", default=0)
    calories_per_serving = models.DecimalField(
        "ккал на порцию",
        max_digits=8,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    calories_per_100g = models.DecimalField(
        "ккал на 100 г",
        max_digits=8,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    proteins_per_serving = models.DecimalField(
        "белки на порцию, г", max_digits=8, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    fats_per_serving = models.DecimalField(
        "жиры на порцию, г", max_digits=8, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    carbohydrates_per_serving = models.DecimalField(
        "углеводы на порцию, г", max_digits=8, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    proteins_per_100g = models.DecimalField(
        "белки на 100 г", max_digits=8, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    fats_per_100g = models.DecimalField(
        "жиры на 100 г", max_digits=8, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    carbohydrates_per_100g = models.DecimalField(
        "углеводы на 100 г", max_digits=8, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    calories_estimated = models.BooleanField(
        "калорийность рассчитана автоматически",
        default=False,
        editable=False,
    )
    nutrition_manual_fields = models.JSONField(default=list, editable=False)
    cover = models.ImageField(
        "фотография блюда",
        upload_to="recipes/covers/%Y/%m/",
        blank=True,
        validators=[validate_recipe_image],
    )
    cover_imported = models.BooleanField(default=False, editable=False)
    status = models.CharField(
        "статус",
        max_length=16,
        choices=Status.choices,
        default=Status.PUBLISHED,
        db_index=True,
    )
    source_url = models.URLField("источник", max_length=2048, blank=True)
    categories = models.ManyToManyField(
        Category,
        blank=True,
        related_name="recipes",
        verbose_name="категории",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_recipes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "title"]
        indexes = [
            GinIndex(
                fields=["title"],
                name="recipe_title_trgm",
                opclasses=["gin_trgm_ops"],
            ),
            GinIndex(
                fields=["description"],
                name="recipe_desc_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ]
        verbose_name = "рецепт"
        verbose_name_plural = "рецепты"

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug or not self.slug.isascii():
            base = recipe_slug_base(self.title)
            candidate = base
            suffix = 2
            while Recipe.objects.exclude(pk=self.pk).filter(slug=candidate).exists():
                candidate = f"{base}-{suffix}"
                suffix += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("recipe-detail", kwargs={"slug": self.slug})

    @property
    def total_minutes(self):
        return self.prep_minutes + self.cook_minutes

    @property
    def is_draft(self):
        return self.status == self.Status.DRAFT


class RecipeSlugAlias(models.Model):
    recipe = models.ForeignKey(
        Recipe, on_delete=models.CASCADE, related_name="slug_aliases"
    )
    slug = models.SlugField(max_length=220, unique=True, allow_unicode=True)

    def __str__(self):
        return self.slug


class RecipeIngredient(models.Model):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="ingredients")
    section = models.CharField(
        "раздел",
        max_length=120,
        blank=True,
        help_text="Например, «Для супа» или «Для гренок».",
    )
    name = models.CharField("ингредиент", max_length=180)
    quantity = models.DecimalField(
        "количество",
        max_digits=9,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    unit = models.CharField("единица", max_length=40, blank=True)
    note = models.CharField("примечание", max_length=240, blank=True)
    search_query = models.CharField(
        "запрос для Лавки",
        max_length=240,
        blank=True,
        help_text="Если оставить пустым, будет использовано название ингредиента.",
    )
    optional = models.BooleanField("необязательный", default=False)
    is_pantry = models.BooleanField(
        "приправа, специя или продукт из запасов",
        default=False,
        help_text="По умолчанию не включается в корзину.",
    )
    estimated = models.BooleanField(
        "количество примерное",
        default=False,
        help_text="Отметьте, если точного количества не было в источнике.",
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "pk"]
        indexes = [
            GinIndex(
                fields=["name"],
                name="ingredient_name_trgm",
                opclasses=["gin_trgm_ops"],
            )
        ]
        verbose_name = "ингредиент"
        verbose_name_plural = "ингредиенты"

    def __str__(self):
        return self.name

    @property
    def effective_search_query(self):
        return self.search_query.strip() or self.name.strip()

    @property
    def is_water(self):
        return is_water_ingredient_name(self.name)


class RecipeStep(models.Model):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="steps")
    section = models.CharField(
        "часть блюда",
        max_length=120,
        blank=True,
        help_text="Например, «Суп» или «Гренки».",
    )
    title = models.CharField("заголовок", max_length=180, blank=True)
    instruction = models.TextField("инструкция")
    image = models.ImageField(
        "фотография шага",
        upload_to="recipes/steps/%Y/%m/",
        blank=True,
        validators=[validate_recipe_image],
    )
    image_imported = models.BooleanField(default=False, editable=False)
    video_timestamp_seconds = models.PositiveIntegerField(
        "тайм-код видео, секунды",
        null=True,
        blank=True,
        editable=False,
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "pk"]
        verbose_name = "шаг"
        verbose_name_plural = "шаги"

    def __str__(self):
        return self.title or f"Шаг {self.order + 1}"

    @property
    def video_timestamp_label(self):
        if self.video_timestamp_seconds is None:
            return ""
        hours, remainder = divmod(self.video_timestamp_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


class ImportJob(models.Model):
    class SourceType(models.TextChoices):
        WEBSITE = "website", "Сайт"
        YOUTUBE = "youtube", "YouTube"

    class Status(models.TextChoices):
        PENDING = "pending", "В очереди"
        PROCESSING = "processing", "Обрабатывается"
        COMPLETED = "completed", "Готово"
        FAILED = "failed", "Ошибка"

    source_url = models.URLField("ссылка", max_length=2048)
    custom_prompt = models.TextField(
        "пожелания к импорту",
        blank=True,
        help_text="Дополнительные требования к адаптации рецепта.",
    )
    source_type = models.CharField("тип источника", max_length=16, choices=SourceType.choices)
    status = models.CharField(
        "статус",
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    recipe = models.OneToOneField(
        Recipe,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_job",
    )
    recipes = models.ManyToManyField(
        Recipe,
        blank=True,
        related_name="import_jobs",
        verbose_name="созданные рецепты",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="recipe_imports",
    )
    source_title = models.CharField("название источника", max_length=300, blank=True)
    source_title_checked_at = models.DateTimeField(
        "последняя проверка названия источника",
        null=True,
        blank=True,
        db_index=True,
    )
    error = models.TextField("ошибка", blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "импорт рецепта"
        verbose_name_plural = "импорты рецептов"

    def __str__(self):
        return f"{self.get_source_type_display()}: {self.source_url}"


class RecipeRefinement(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "В очереди"
        PROCESSING = "processing", "Обрабатывается"
        COMPLETED = "completed", "Готово"
        FAILED = "failed", "Ошибка"

    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name="refinements",
        verbose_name="рецепт",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="recipe_refinements",
    )
    prompt = models.TextField("пожелание")
    status = models.CharField(
        "статус",
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    expected_recipe_updated_at = models.DateTimeField(
        "версия рецепта перед обработкой"
    )
    error = models.TextField("ошибка", blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["recipe"],
                condition=models.Q(status__in=["pending", "processing"]),
                name="one_active_refinement_per_recipe",
            )
        ]
        verbose_name = "пожелание к рецепту"
        verbose_name_plural = "пожелания к рецептам"

    def __str__(self):
        return f"{self.recipe}: {self.prompt[:80]}"


class StorePreference(models.Model):
    class Store(models.TextChoices):
        AUCHAN = "auchan", "Ашан"
        PEREKRESTOK = "perekrestok", "Перекрёсток"
        PYATEROCHKA = "pyaterochka", "Пятёрочка"
        MAGNIT = "magnit", "Магнит"
        LAVKA = "lavka", "Яндекс Лавка"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="store_preferences",
    )
    store = models.CharField("магазин", max_length=24, choices=Store.choices)
    position = models.PositiveSmallIntegerField("приоритет", default=0)
    enabled = models.BooleanField("использовать", default=True)
    legacy_enabled_before_single_selection = models.BooleanField(
        null=True,
        blank=True,
        editable=False,
    )

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "store"],
                name="unique_store_preference_per_user",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(enabled=True),
                name="unique_selected_store_per_user",
            ),
        ]
        verbose_name = "магазин пользователя"
        verbose_name_plural = "магазины пользователей"

    def __str__(self):
        return f"{self.user}: {self.get_store_display()}"


class CartRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "В очереди"
        PROCESSING = "processing", "Собирается"
        COMPLETED = "completed", "Корзина готова"
        REVIEW = "review", "Нужна проверка"
        CONFIRMED = "confirmed", "Подтверждена"
        CLEANUP_PENDING = "cleanup_pending", "Ожидает очистки"
        CLEANING = "cleaning", "Очищается"
        MANUAL_CHECK = "manual_check", "Нужна ручная проверка"
        CANCELLED = "cancelled", "Отменена"
        LOGIN_REQUIRED = "login_required", "Нужен вход"
        FAILED = "failed", "Ошибка"

    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="cart_runs")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="cart_runs",
    )
    servings = models.PositiveSmallIntegerField("порций")
    status = models.CharField(
        "статус",
        max_length=24,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    store_priority = models.JSONField("очередь магазинов", default=list)
    ingredient_snapshot = models.JSONField("ингредиенты", default=list)
    next_store_index = models.PositiveSmallIntegerField(default=0)
    selected_attempt = models.ForeignKey(
        "CartAttempt",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="selected_for_runs",
    )
    error = models.TextField("ошибка", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    confirmation_deadline = models.DateTimeField(null=True, blank=True, db_index=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cleanup_requested_at = models.DateTimeField(null=True, blank=True)
    cleaned_at = models.DateTimeField(null=True, blank=True)
    cancellation_requested_at = models.DateTimeField(null=True, blank=True)
    browser_operation_started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "сборка корзины"
        verbose_name_plural = "сборки корзин"

    def __str__(self):
        return f"{self.recipe}: {self.get_status_display()}"

    @property
    def is_active(self):
        return self.status in {
            self.Status.PENDING,
            self.Status.PROCESSING,
            self.Status.CLEANUP_PENDING,
            self.Status.CLEANING,
            self.Status.MANUAL_CHECK,
        }

    @property
    def can_stop(self):
        return self.status in {
            self.Status.PENDING,
            self.Status.PROCESSING,
            self.Status.CLEANUP_PENDING,
            self.Status.CLEANING,
            self.Status.MANUAL_CHECK,
            self.Status.LOGIN_REQUIRED,
        }

    @property
    def stop_is_pending(self):
        return bool(
            self.cancellation_requested_at
            and self.status in {self.Status.PROCESSING, self.Status.CLEANING}
        )


class BrowserLoginSession(models.Model):
    class Status(models.TextChoices):
        STARTING = "starting", "Запускается"
        ACTIVE = "active", "Открыта"
        STOPPING = "stopping", "Закрывается"
        COMPLETING = "completing", "Сохраняется"
        COMPLETED = "completed", "Сохранена"
        EXPIRED = "expired", "Истекла"
        FAILED = "failed", "Ошибка"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="browser_login_sessions",
    )
    run = models.ForeignKey(
        CartRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="browser_login_sessions",
    )
    remote_session_id = models.CharField(max_length=128, blank=True, unique=True, null=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.STARTING,
        db_index=True,
    )
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    transition_started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "ручной вход в браузер"
        verbose_name_plural = "ручные входы в браузер"

    def __str__(self):
        return f"{self.user}: {self.get_status_display()}"


class ApplicationLock(models.Model):
    """A database row used to serialize cross-process application operations."""

    name = models.CharField(max_length=32, primary_key=True)

    class Meta:
        verbose_name = "блокировка приложения"
        verbose_name_plural = "блокировки приложения"

    def __str__(self):
        return self.name


class RegistrationInvite(models.Model):
    token_digest = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_registration_invites",
    )
    registered_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="registration_invite",
    )
    is_open = models.BooleanField(default=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "приглашение в семейную книгу"
        verbose_name_plural = "приглашения в семейную книгу"
        constraints = [
            models.UniqueConstraint(
                fields=["is_open"],
                condition=models.Q(is_open=True),
                name="unique_open_registration_invite",
            )
        ]

    @staticmethod
    def digest_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def __str__(self):
        return f"Приглашение от {self.created_by or 'владельца'}"


class CartAttempt(models.Model):
    class Status(models.TextChoices):
        PROCESSING = "processing", "Поиск"
        EXACT = "exact", "Все продукты найдены"
        SUBSTITUTIONS = "substitutions", "Есть замены"
        INCOMPLETE = "incomplete", "Не всё найдено"
        LOGIN_REQUIRED = "login_required", "Нужен вход"
        BLOCKED = "blocked", "Сайт заблокировал браузер"
        FAILED = "failed", "Ошибка"

    run = models.ForeignKey(CartRun, on_delete=models.CASCADE, related_name="attempts")
    store = models.CharField("магазин", max_length=24, choices=StorePreference.Store.choices)
    status = models.CharField(
        "статус",
        max_length=24,
        choices=Status.choices,
        default=Status.PROCESSING,
    )
    cart_url = models.URLField("ссылка на корзину", max_length=2048, blank=True)
    summary = models.CharField("результат", max_length=500, blank=True)
    result = models.JSONField("ответ агента", default=dict, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["started_at"]
        constraints = [
            models.UniqueConstraint(fields=["run", "store"], name="unique_store_attempt_per_run")
        ]
        verbose_name = "попытка сборки корзины"
        verbose_name_plural = "попытки сборки корзины"

    def __str__(self):
        return f"{self.get_store_display()}: {self.get_status_display()}"


class CartItemMatch(models.Model):
    class MatchQuality(models.TextChoices):
        EXACT = "exact", "Точное совпадение"
        SUBSTITUTE = "substitute", "Замена"
        MISSING = "missing", "Не найдено"

    attempt = models.ForeignKey(CartAttempt, on_delete=models.CASCADE, related_name="matches")
    ingredient_name = models.CharField("ингредиент", max_length=180)
    requested_quantity = models.CharField("нужно", max_length=80, blank=True)
    product_name = models.CharField("выбранный товар", max_length=300, blank=True)
    product_url = models.URLField("ссылка на товар", max_length=2048, blank=True)
    package_count = models.PositiveSmallIntegerField("упаковок", default=0)
    quality = models.CharField("качество совпадения", max_length=16, choices=MatchQuality.choices)
    warning = models.CharField("предупреждение", max_length=500, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "pk"]
        verbose_name = "подбор товара"
        verbose_name_plural = "подборы товаров"

    def __str__(self):
        return f"{self.ingredient_name}: {self.get_quality_display()}"
