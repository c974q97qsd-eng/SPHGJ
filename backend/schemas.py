"""API 请求/响应模型。"""
from typing import Optional, List
from pydantic import BaseModel, Field


class AutoCommentConfig(BaseModel):
    enabled: bool
    content: str = ""


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    auto_comment_enabled: Optional[bool] = None
    auto_comment_content: Optional[str] = None


class ManualReply(BaseModel):
    account_id: str
    content: str = Field(..., min_length=1)


class DeleteCommentBody(BaseModel):
    account_id: str
    export_id: str


class BatchDeleteItem(BaseModel):
    comment_id: str
    account_id: str
    export_id: str


class BatchDeleteBody(BaseModel):
    items: List[BatchDeleteItem]


class LoginFinalize(BaseModel):
    account_id: Optional[str] = None
    name: Optional[str] = None


class ConfigUpdate(BaseModel):
    fetch_interval_sec: Optional[int] = Field(default=None, ge=10)
    auto_reply_enabled: Optional[bool] = None
    card_fields: Optional[List[str]] = None
    dashboard_interval_sec: Optional[int] = Field(default=None, ge=10)
    live_check_interval_sec: Optional[int] = Field(default=None, ge=2)
    manual_release_delay_sec: Optional[int] = Field(default=None, ge=0)


class AutoReplyRule(BaseModel):
    keyword: str
    reply: str = ""


class AutoReplyConfig(BaseModel):
    enabled: bool = False
    rules: List[AutoReplyRule] = []


class AutoDeleteConfig(BaseModel):
    enabled: bool = False
    keywords: List[str] = []


class PinCommentBody(BaseModel):
    account_id: str
    export_id: str
    op_type: int = 1  # 1 置顶 / 0 取消
