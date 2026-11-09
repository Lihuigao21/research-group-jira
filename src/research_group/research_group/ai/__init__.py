"""受控的课题组助手；权限与写入确认由应用负责。"""

AI_DOCTYPES = (
    "RG AI State", "RG AI Conversation", "RG AI Message", "RG AI Memory",
    "RG AI Plan", "RG AI Operation", "RG AI Run", "RG AI Tombstone",
)
CONTENT_DOCTYPES = AI_DOCTYPES[1:7]
TOKEN = object()
