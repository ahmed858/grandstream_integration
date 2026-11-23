"""
Structured output that Gemini must return for every analysed call.

Keep the field names stable: they are mapped 1:1 to the custom fields
on CRM Call Log in analysis._save().
"""
from typing import Literal

from pydantic import BaseModel, Field

SERVICES = ("مزاد", "تسويق عقار", "تسويق مشاريع", "إدارة أملاك", "تقييم", "أخرى")
CUSTOMER_TYPES = ("مستثمر", "مستهلك", "بائع", "بنك", "جهة حكومية", "أخرى")
INTEREST = ("مهتم", "غير مهتم")


class CriterionScore(BaseModel):
    criterion: str = Field(description="اسم المعيار كما هو مكتوب في التعليمات بالظبط")
    score: int = Field(description="الدرجة من 1 إلى 5")
    reason: str = Field(description="سبب الدرجة في جملة، ومعاها اقتباس قصير من كلام المكالمة")


class Objection(BaseModel):
    objection: str = Field(description="اعتراض العميل بكلامه")
    marketer_response: str = Field(description="رد المسوق باختصار، أو 'لم يرد' لو تجاهله")
    handled_well: bool


class CallAnalysis(BaseModel):
    summary: str = Field(description="ملخص محايد للمكالمة في 2 إلى 3 جمل، من غير درجات أو حكم على المسوق")
    service: Literal[SERVICES] = Field(description="الخدمة اللي المكالمة عنها")
    customer_type: Literal[CUSTOMER_TYPES]
    interested: Literal[INTEREST]
    not_interested_check: str | None = Field(
        default=None,
        description=(
            "لو العميل غير مهتم فقط: السبب اللي قاله، وهل هو فعلاً غير مهتم "
            "ولا المسوق خسره بأسلوبه أو لأنه ما فهمش احتياجه. سيبه فاضي لو مهتم."
        ),
    )
    customer_profile: str = Field(
        description="احتياج العميل وميزانيته وتوقيته والمنطقة لو اتذكروا. اكتب 'غير مذكور' للي ما اتقالش"
    )
    next_step: str | None = Field(default=None, description="الخطوة الجاية المتفق عليها، لو فيه")
    next_step_date: str | None = Field(
        default=None, description="تاريخ الخطوة الجاية بصيغة YYYY-MM-DD لو اتحدد يوم واضح، وإلا فاضي"
    )
    success: bool = Field(description="هل المكالمة حققت هدفها حسب معيار النجاح في التعليمات")
    scores: list[CriterionScore] = Field(description="درجة لكل معيار من معايير التقييم بالترتيب")
    objections: list[Objection] = Field(default_factory=list)
    coaching: list[str] = Field(
        description="أهم 3 نقط يحسنها المسوق، كل نقطة جملة موجهة له مباشرة: كان ممكن تعمل كذا"
    )
