"""通行保障服务的错误类型。"""

from __future__ import annotations


class MobilityError(Exception):
    """所有领域错误的基类，携带稳定代码与现场引用。"""

    code = "mobility_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        field: str | None = None,
        demand_id: str | None = None,
        revision_id: str | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
        self.field = field
        self.demand_id = demand_id
        self.revision_id = revision_id

    def to_dict(self) -> dict:
        data = {"code": self.code, "message": self.message}
        if self.field:
            data["field"] = self.field
        if self.demand_id:
            data["demand_id"] = self.demand_id
        if self.revision_id:
            data["revision_id"] = self.revision_id
        return data


class DomainRejection(MobilityError):
    """请求被领域规则拒绝（调用方可修正后重试）。"""

    code = "domain_rejection"


class ValidationRejection(DomainRejection):
    code = "validation_rejected"


class FactVersionUnknown(DomainRejection):
    code = "fact_version_unknown"


class CapacityExhausted(DomainRejection):
    code = "capacity_exhausted"

    def __init__(self, message: str, *, resource_ref: str | None = None, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.resource_ref = resource_ref

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.resource_ref:
            data["resource_ref"] = self.resource_ref
        return data


class AccessForbidden(DomainRejection):
    code = "access_forbidden"


class RoleForbidden(DomainRejection):
    code = "role_forbidden"


class IllegalTransition(DomainRejection):
    code = "illegal_transition"


class ReceiptConflict(DomainRejection):
    code = "receipt_conflict"


class EmergencyOverdue(DomainRejection):
    code = "emergency_overdue"


class EventContractBroken(MobilityError):
    """服务内部产生的事件未通过自身契约，属于程序缺陷而非调用方错误。"""

    code = "event_contract_broken"
