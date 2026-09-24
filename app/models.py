"""Pydantic request/response models."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from .importer import MAX_IMPORT_ITEMS
from .irt import THETA_MAX, THETA_MIN

# JSON booleans must not sneak in as quota values; strict ints reject them.
PositiveIntField = Annotated[int, Field(strict=True, gt=0)]


# ---- items ----------------------------------------------------------------

class OptionOut(BaseModel):
    key: str
    text: str


class ItemOut(BaseModel):
    """An item as served to an examinee (the correct answer is not exposed)."""

    id: str
    difficulty: float
    discrimination: float
    content_tag: str | None = None
    stem: str
    options: list[OptionOut]


class BankItemOut(BaseModel):
    """Item bank row for administration/inspection, including exposure."""

    id: str
    difficulty: float
    discrimination: float
    content_tag: str | None = None
    stem: str
    answer: str
    exposure_count: int
    is_active: bool = Field(
        ..., description="是否启用：false 表示已撤回，不再参与新取题"
    )


class ItemAvailabilityUpdate(BaseModel):
    """PATCH body enabling or withdrawing one bank item."""

    is_active: bool = Field(
        ...,
        strict=True,
        description="true=启用（参与新取题）；false=撤回（不再参与新取题）",
    )


class ItemDetailOut(BankItemOut):
    options: list[OptionOut]


class ItemBankOut(BaseModel):
    count: int
    items: list[BankItemOut]


class ItemAnalysisOut(BaseModel):
    """Post-hoc quality review of one item, computed from finished sessions."""

    item_id: str = Field(..., description="被复盘的题目 ID")
    sample_count: int = Field(
        ...,
        description=(
            "有效样本数：已结束（finished）会话中，答过该题且同场至少另答"
            "一题的会话数量"
        ),
    )
    correct_rate: float | None = Field(
        None,
        description=(
            "该题在有效样本中的历史正确率（分子为作答时保存的 score 之和）；"
            "无有效样本时为 null"
        ),
    )
    pearson_correlation: float | None = Field(
        None,
        description=(
            "目标题得分（0/1）与同场其余题答对比例的 Pearson 相关系数；"
            "样本少于 5 或任一序列无方差（相关系数未定义）时为 null"
        ),
    )


# ---- item bulk import ------------------------------------------------------

class ItemImportRequest(BaseModel):
    """Bulk import envelope: an item array plus the dry-run switch."""

    # ``Any`` (not ``dict``) on purpose: malformed rows (scalars/lists) must
    # reach :mod:`app.importer` so they are reported per-row as
    # ``invalid_row`` instead of failing the whole request at the boundary.
    items: list[Any] = Field(
        ...,
        min_length=1,
        max_length=MAX_IMPORT_ITEMS,
        description=(
            "待导入题目数组（1~"
            f"{MAX_IMPORT_ITEMS} 行），行号从 1 开始。每行对象包含："
            "id（非空白字符串主键）、difficulty（[-6,6] 有限数）、"
            "discrimination（(0,5] 正有限数）、content_tag（字符串或 null，"
            "空串归一化为 null）、stem（非空白题干）、"
            "options（2~8 个 {key,text} 对象，键本行唯一、自动去空白并大写）、"
            "answer（必须是 options 中出现过的键）。逐行语义校验由服务端完成，"
            "不会在第一个坏行处中断整批请求。"
        ),
    )
    dry_run: bool = Field(
        False,
        strict=True,
        description="预检开关：true 时只校验并报告 created/updated 计划，不写库",
    )


class ItemImportRowOut(BaseModel):
    """Per-row outcome of a successful (or dry-run) import, keyed by stable row."""

    row: int = Field(..., description="该题在请求 items 数组中的 1 基行号")
    id: str
    action: Literal["created", "updated"] = Field(
        ..., description="created=新增题目；updated=按 ID 更新既有题目（保留曝光次数）"
    )


class ItemImportErrorOut(BaseModel):
    """One validation error, attached to a stable 1-based row number."""

    row: int = Field(..., description="出错行在请求 items 数组中的 1 基行号")
    code: str = Field(
        ...,
        description=(
            "错误代码，如 missing_field / invalid_type / out_of_range / "
            "duplicate_option_key / answer_not_in_options / "
            "duplicate_id_in_batch / invalid_row"
        ),
    )
    message: str = Field(..., description="可读错误说明（中文）")
    field: str | None = Field(
        None, description="出错字段名（options 为数组级错误），行级错误为 null"
    )
    id: str | None = Field(None, description="若能解析出题目 ID 则一并返回")


class ItemImportResponse(BaseModel):
    dry_run: bool = Field(..., description="回显请求的预检开关")
    valid: bool = Field(
        ...,
        description="整批是否通过全部逐行校验；false 时正式导入未写任何数据",
    )
    created_count: int = Field(
        ..., description="新增题目数（dry_run 时为预检计划数；有错误时为 0）"
    )
    updated_count: int = Field(
        ..., description="更新题目数（dry_run 时为预检计划数；有错误时为 0）"
    )
    total: int = Field(..., description="提交的行数")
    results: list[ItemImportRowOut] = Field(
        default_factory=list,
        description="逐行处理结果，按稳定行号升序；有错误或 dry_run 见各字段说明",
    )
    errors: list[ItemImportErrorOut] = Field(
        default_factory=list, description="逐行错误，按稳定行号升序"
    )


# ---- sessions -------------------------------------------------------------

class SessionCreate(BaseModel):
    initial_theta: float = Field(
        0.0, ge=THETA_MIN, le=THETA_MAX, description="初始能力值"
    )
    max_items: int = Field(
        10, ge=1, le=50, description="本次测验最多作答题目数"
    )
    exposure_limit: int | None = Field(
        None,
        ge=1,
        le=10_000,
        description="单题全局曝光上限；缺省取服务端 DEFAULT_EXPOSURE_LIMIT",
    )
    duration_seconds: int | None = Field(
        None,
        ge=60,
        le=86_400,
        description=(
            "可选测验时限（秒，60~86400）；"
            "服务端按 UTC 记录截止时间，到期后 /next 与提交答案返回 409"
        ),
    )
    tag_minimums: dict[str, PositiveIntField] = Field(
        default_factory=dict,
        description=(
            "可选内容标签最低答题配额：键须为题库现有非空 content_tag，"
            "值为正整数，所有配额之和不超过 max_items；不传则不设配额"
        ),
    )
    target_se: float | None = Field(
        None,
        gt=0.0,
        allow_inf_nan=False,
        description=(
            "可选能力估计精度停测目标标准误（正有限值，SE = 1/√Σ Fisher 信息）；"
            "须与 min_precision_items 同时提供，不传则不按精度停测"
        ),
    )
    min_precision_items: PositiveIntField | None = Field(
        None,
        ge=1,
        le=50,
        description=(
            "触发精度停测前至少需要作答的题数（1～max_items）；"
            "须与 target_se 同时提供"
        ),
    )

    @model_validator(mode="after")
    def _validate_precision_stop(self) -> SessionCreate:
        if (self.target_se is None) != (self.min_precision_items is None):
            raise ValueError(
                "target_se 与 min_precision_items 必须同时提供或同时省略"
            )
        if (
            self.min_precision_items is not None
            and self.min_precision_items > self.max_items
        ):
            raise ValueError("min_precision_items 不能大于 max_items")
        return self


class SessionOut(BaseModel):
    id: str
    theta: float
    current_item_id: str | None
    max_items: int
    exposure_limit: int
    duration_seconds: int | None = None
    expires_at: str | None = None
    paused: bool = Field(
        False, description="会话是否处于暂停状态（暂停期间不消耗时限、不能取题或作答）"
    )
    paused_at: str | None = Field(
        None, description="本次暂停开始的 UTC 时间；未暂停时为 null"
    )
    remaining_seconds: int | None = Field(
        None,
        description=(
            "测验剩余秒数：暂停时为暂停瞬间冻结的值；进行中为按 UTC 实时计算的值；"
            "无时限会话为 null"
        ),
    )
    tag_minimums: dict[str, int] = Field(default_factory=dict)
    target_se: float | None = Field(
        None, description="精度停测目标标准误；未启用时为 null"
    )
    min_precision_items: int | None = Field(
        None, description="精度停测最少作答题数；未启用时为 null"
    )
    standard_error: float | None = Field(
        None, description="当前 θ 下全部已答题的估计标准误 1/√Σ信息；无已答题为 null"
    )
    status: str
    finish_reason: str | None
    answered_count: int
    created_at: str
    updated_at: str


class NextItemOut(BaseModel):
    session_id: str
    status: str
    item: ItemOut | None = None
    information: float | None = None
    exposure_count: int | None = None
    exposure_limit: int
    answered_count: int
    max_items: int
    paused: bool = False
    paused_at: str | None = None
    remaining_seconds: int | None = None
    target_se: float | None = None
    min_precision_items: int | None = None
    standard_error: float | None = None
    finish_reason: str | None = None
    note: str | None = None


# ---- responses ------------------------------------------------------------

class ResponseCreate(BaseModel):
    response_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="客户端生成的幂等键，建议使用 UUID",
    )
    item_id: str = Field(..., description="刚从 /next 取得的题目 ID")
    response: str = Field(..., min_length=1, description="作答选项键，如 'A'")


class ResponseOut(BaseModel):
    id: str
    session_id: str
    item_id: str
    response: str
    correct: bool
    score: int
    correct_answer: str
    theta_before: float
    theta_after: float
    theta_change: float
    p_correct: float
    information_at_theta: float
    exposure_count: int
    created_at: str


class ResponseHistoryOut(BaseModel):
    session_id: str
    status: str
    theta: float
    count: int
    results: list[ResponseOut]


# ---- progress -------------------------------------------------------------

class ProgressOut(BaseModel):
    session_id: str
    status: str
    theta: float
    answered_count: int
    max_items: int
    bank_size: int
    percent_complete: float
    exposure_limit: int
    duration_seconds: int | None = None
    expires_at: str | None = None
    paused: bool = False
    paused_at: str | None = None
    remaining_seconds: int | None = None
    tag_minimums: dict[str, int] = Field(
        default_factory=dict, description="各内容标签的最低答题配额"
    )
    tag_answered: dict[str, int] = Field(
        default_factory=dict, description="各内容标签已答题数（按答题记录统计）"
    )
    target_se: float | None = Field(
        None, description="精度停测目标标准误；未启用时为 null"
    )
    min_precision_items: int | None = Field(
        None, description="精度停测最少作答题数；未启用时为 null"
    )
    standard_error: float | None = Field(
        None, description="当前 θ 下的能力估计标准误；无已答题或总信息为零时为 null"
    )
    finish_reason: str | None
    current_item: ItemOut | None
    answered_item_ids: list[str]


class HealthOut(BaseModel):
    status: str
    bank_size: int
