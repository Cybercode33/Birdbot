"""Editable bilingual command messages.

The JSON file is read again whenever its modification time changes. This lets
an administrator adjust wording on the bot host without editing Python or
restarting the process. Missing or malformed entries fall back to the built-in
English defaults so a typo in the file cannot stop a command from running.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


COMMAND_MESSAGES_PATH = Path(__file__).resolve().parent / "config" / "commands.config.json"

DEFAULT_COMMAND_MESSAGES: dict[str, dict[str, dict[str, str]]] = {
    "common": {
        "en": {
            "disabled": "This command is disabled for this server. Enable it from the website Commands tab.",
            "not_enabled": "This server has not enabled BirdBot yet.",
            "bad_argument": "I could not understand that command. Check the member and options, then try again.",
            "failed": "BirdBot could not complete that command. Please try again.",
            "slash_option": "I could not understand that command option.",
            "cooldown": "Please wait before using that command again.",
            "permission": "You do not have permission to use that command.",
        },
        "ar": {
            "disabled": "هذا الأمر معطّل لهذا الخادم. فعّله من تبويب الأوامر في الموقع.",
            "not_enabled": "لم يفعّل هذا الخادم BirdBot بعد.",
            "bad_argument": "لم أفهم هذا الأمر. تحقق من العضو والخيارات ثم حاول مجدداً.",
            "failed": "تعذر على BirdBot تنفيذ هذا الأمر. حاول مجدداً.",
            "slash_option": "لم أفهم خيار هذا الأمر.",
            "cooldown": "يرجى الانتظار قبل استخدام هذا الأمر مرة أخرى.",
            "permission": "ليس لديك صلاحية استخدام هذا الأمر.",
        },
    },
    "ping": {
        "en": {
            "checking": "Checking connection...",
            "title": "Connection check",
            "description": "BirdBot is online and responding.",
            "response": "Response",
            "gateway": "Gateway",
            "footer": "Requested from dashboard by {requested_by}",
        },
        "ar": {
            "checking": "جارٍ التحقق من الاتصال...",
            "title": "فحص الاتصال",
            "description": "BirdBot متصل ويستجيب.",
            "response": "الاستجابة",
            "gateway": "البوابة",
            "footer": "طُلب من لوحة التحكم بواسطة {requested_by}",
        },
    },
    "server": {
        "en": {
            "title": "{name} server info",
            "server_id": "Server ID",
            "owner": "Owner",
            "members": "Members",
            "created": "Created",
            "boost_level": "Boost level",
            "boost_value": "Level {level}",
            "unavailable": "Unavailable",
        },
        "ar": {
            "title": "معلومات خادم {name}",
            "server_id": "معرّف الخادم",
            "owner": "المالك",
            "members": "الأعضاء",
            "created": "تاريخ الإنشاء",
            "boost_level": "مستوى التعزيز",
            "boost_value": "المستوى {level}",
            "unavailable": "غير متاح",
        },
    },
    "profile": {
        "en": {
            "title": "{name} profile",
            "user_id": "User ID",
            "account_created": "Account created",
            "joined_server": "Joined server",
            "roles": "Roles",
            "no_roles": "No roles",
            "unavailable": "Unavailable",
        },
        "ar": {
            "title": "ملف {name}",
            "user_id": "معرّف المستخدم",
            "account_created": "تاريخ إنشاء الحساب",
            "joined_server": "انضم إلى الخادم",
            "roles": "الأدوار",
            "no_roles": "لا توجد أدوار",
            "unavailable": "غير متاح",
        },
    },
    "kick": {
        "en": {
            "success": "Kicked {member}\n\n> Reason: {reason}",
            "permission": "You need Kick Members permission.",
            "dashboard_permission": "BirdBot needs the Kick Members permission.",
            "missing_member": "Choose a member first.",
            "no_reason": "No reason provided.",
        },
        "ar": {
            "success": "طرد {member}\n\n> السبب: {reason}",
            "permission": "تحتاج إلى صلاحية طرد الأعضاء.",
            "dashboard_permission": "يحتاج BirdBot إلى صلاحية طرد الأعضاء.",
            "missing_member": "اختر عضواً أولاً.",
            "no_reason": "لم يتم تقديم سبب.",
        },
    },
    "ban": {
        "en": {
            "success": "Banned {member}\n\n> Reason: {reason}",
            "permission": "You need Ban Members permission.",
            "dashboard_permission": "BirdBot needs the Ban Members permission.",
            "missing_member": "Choose a member first.",
            "no_reason": "No reason provided.",
        },
        "ar": {
            "success": "حظر {member}\n\n> السبب: {reason}",
            "permission": "تحتاج إلى صلاحية حظر الأعضاء.",
            "dashboard_permission": "يحتاج BirdBot إلى صلاحية حظر الأعضاء.",
            "missing_member": "اختر عضواً أولاً.",
            "no_reason": "لم يتم تقديم سبب.",
        },
    },
    "warning": {
        "en": {
            "success": "Warning {member}\n\n> Reason: {reason}\n> Warning number: #{number}",
            "dm": "You have received a warning.\n\n> Warning number: #{number}\n> Server: {server}\n> Reason: {reason}",
            "permission": "You need the Moderate Members permission to manage warnings.",
            "missing_member": "Choose a member first.",
            "missing_reason": "Provide a reason for the warning.",
            "no_reason": "No reason provided.",
            "dashboard_permission": "BirdBot needs the Moderate Members permission to manage warnings.",
            "dm_failed": " The warning was saved, but I could not send the member a DM.",
        },
        "ar": {
            "success": "تحذير {member}\n\n> السبب: {reason}\n> رقم التحذير: #{number}",
            "dm": "لقد تلقيت تحذيرًا.\n\n> رقم التحذير: #{number}\n> الخادم: {server}\n> السبب: {reason}",
            "permission": "تحتاج إلى صلاحية إدارة الأعضاء لإدارة التحذيرات.",
            "missing_member": "اختر عضواً أولاً.",
            "missing_reason": "اكتب سبب التحذير.",
            "no_reason": "لا يوجد سبب محدد.",
            "dashboard_permission": "يحتاج BirdBot إلى صلاحية إدارة الأعضاء لإدارة التحذيرات.",
            "dm_failed": " تم حفظ التحذير، لكن تعذر إرسال رسالة خاصة للعضو.",
        },
    },
    "unwarning": {
        "en": {
            "success": "Warning #{number} for {member} was removed.",
            "not_found": "No active warning with number #{number} was found in this server.",
            "permission": "You need the Moderate Members permission to manage warnings.",
            "missing_number": "Enter the warning number to remove.",
            "invalid_number": "Enter a valid warning number.",
            "dashboard_permission": "BirdBot needs the Moderate Members permission to manage warnings.",
        },
        "ar": {
            "success": "تمت إزالة التحذير رقم #{number} عن {member}.",
            "not_found": "لم يتم العثور على تحذير نشط بالرقم #{number} في هذا الخادم.",
            "permission": "تحتاج إلى صلاحية إدارة الأعضاء لإدارة التحذيرات.",
            "missing_number": "اكتب رقم التحذير الذي تريد إزالته.",
            "invalid_number": "اكتب رقم تحذير صحيحاً.",
            "dashboard_permission": "يحتاج BirdBot إلى صلاحية إدارة الأعضاء لإدارة التحذيرات.",
        },
    },
    "show_warning": {
        "en": {
            "title": "Warnings for {member}",
            "count": "Active warnings: {count}",
            "none": "{member} has no active warnings.",
            "entry": "Warning #{number} • {date}\nModerator: {moderator}\nReason: {reason}",
            "permission": "You need the Moderate Members permission to view warnings.",
            "missing_member": "Choose a member first.",
        },
        "ar": {
            "title": "تحذيرات {member}",
            "count": "التحذيرات النشطة: {count}",
            "none": "لا توجد تحذيرات نشطة على {member}.",
            "entry": "التحذير #{number} • {date}\nالمشرف: {moderator}\nالسبب: {reason}",
            "permission": "تحتاج إلى صلاحية إدارة الأعضاء لعرض التحذيرات.",
            "missing_member": "اختر عضواً أولاً.",
        },
    },
    "timeout": {
        "en": {
            "success": "Timed out {member}\n\n> Duration: {duration} minute(s)\n> Reason: {reason}",
            "permission": "You need the Moderate Members permission to use timeout.",
            "dashboard_permission": "BirdBot needs the Moderate Members permission to apply timeouts.",
            "missing_member": "Choose a member first.",
            "missing_duration": "Enter the timeout duration in minutes.",
            "invalid_member": "Choose a valid member.",
            "invalid_duration": "Duration must be between 1 minute and 28 days (40,320 minutes).",
            "no_reason": "No reason provided.",
        },
        "ar": {
            "success": "إسكات مؤقت {member}\n\n> المدة: {duration} دقيقة\n> السبب: {reason}",
            "permission": "تحتاج إلى صلاحية إدارة الأعضاء لاستخدام الإسكات المؤقت.",
            "dashboard_permission": "يحتاج BirdBot إلى صلاحية إدارة الأعضاء لتطبيق الإسكات المؤقت.",
            "missing_member": "اختر عضواً أولاً.",
            "missing_duration": "اكتب مدة الإسكات بالدقائق.",
            "invalid_member": "اختر عضواً صحيحاً.",
            "invalid_duration": "يجب أن تكون المدة بين دقيقة واحدة و28 يوماً (40320 دقيقة).",
            "no_reason": "لم يتم تقديم سبب.",
        },
    },
    "lock": {
        "en": {
            "success": "This channel is now locked.",
            "permission": "You need the Manage Channels permission to lock a channel.",
            "dashboard_permission": "BirdBot needs Manage Channels permission to lock this channel.",
            "invalid_channel": "This command can only be used in a text channel.",
        },
        "ar": {
            "success": "تم قفل هذه القناة.",
            "permission": "تحتاج إلى صلاحية إدارة القنوات لقفل القناة.",
            "dashboard_permission": "يحتاج BirdBot إلى صلاحية إدارة القنوات لقفل هذه القناة.",
            "invalid_channel": "يمكن استخدام هذا الأمر في قناة نصية فقط.",
        },
    },
    "unlock": {
        "en": {
            "success": "This channel is now unlocked.",
            "permission": "You need the Manage Channels permission to unlock a channel.",
            "dashboard_permission": "BirdBot needs Manage Channels permission to unlock this channel.",
            "invalid_channel": "This command can only be used in a text channel.",
        },
        "ar": {
            "success": "تم فتح هذه القناة.",
            "permission": "تحتاج إلى صلاحية إدارة القنوات لفتح القناة.",
            "dashboard_permission": "يحتاج BirdBot إلى صلاحية إدارة القنوات لفتح هذه القناة.",
            "invalid_channel": "يمكن استخدام هذا الأمر في قناة نصية فقط.",
        },
    },
    "delete": {
        "en": {
            "success": "Deleted {count} message(s).",
            "permission": "You need the Manage Messages permission to delete messages.",
            "dashboard_permission": "BirdBot needs Manage Messages permission to delete messages.",
            "history_permission": "BirdBot needs Read Message History permission to delete messages.",
            "invalid_channel": "This command can only be used in a text channel.",
            "missing_amount": "Enter how many messages to delete.",
            "invalid_amount": "The amount must be between 1 and 100 messages.",
            "failed": "Discord could not delete those messages. Please try again.",
        },
        "ar": {
            "success": "تم حذف {count} رسالة.",
            "permission": "تحتاج إلى صلاحية إدارة الرسائل لحذف الرسائل.",
            "dashboard_permission": "يحتاج BirdBot إلى صلاحية إدارة الرسائل لحذف الرسائل.",
            "history_permission": "يحتاج BirdBot إلى صلاحية قراءة سجل الرسائل لحذف الرسائل.",
            "invalid_channel": "يمكن استخدام هذا الأمر في قناة نصية فقط.",
            "missing_amount": "اكتب عدد الرسائل التي تريد حذفها.",
            "invalid_amount": "يجب أن يكون العدد بين 1 و100 رسالة.",
            "failed": "تعذر على Discord حذف هذه الرسائل. حاول مرة أخرى.",
        },
    },
    "unban": {
        "en": {"success": "{member} was unbanned."},
        "ar": {"success": "تم إلغاء حظر {member}."},
    },
}

_cached_mtime_ns: int | None = None
_cached_messages: dict[str, dict[str, dict[str, str]]] = DEFAULT_COMMAND_MESSAGES


def _load_messages() -> dict[str, dict[str, dict[str, str]]]:
    global _cached_mtime_ns, _cached_messages
    try:
        mtime_ns = COMMAND_MESSAGES_PATH.stat().st_mtime_ns
    except OSError:
        return _cached_messages
    if _cached_mtime_ns == mtime_ns:
        return _cached_messages
    try:
        payload = json.loads(COMMAND_MESSAGES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        _cached_mtime_ns = mtime_ns
        return _cached_messages
    if not isinstance(payload, dict):
        _cached_mtime_ns = mtime_ns
        return _cached_messages
    merged: dict[str, dict[str, dict[str, str]]] = {
        command: {language: dict(values) for language, values in languages.items()}
        for command, languages in DEFAULT_COMMAND_MESSAGES.items()
    }
    for command, languages in payload.items():
        if command.startswith("_") or not isinstance(languages, dict):
            continue
        merged.setdefault(command, {})
        for language, values in languages.items():
            if language not in {"en", "ar"} or not isinstance(values, dict):
                continue
            merged[command].setdefault(language, {})
            merged[command][language].update(
                {str(key): value for key, value in values.items() if isinstance(value, str)}
            )
    _cached_mtime_ns = mtime_ns
    _cached_messages = merged
    return merged


class _SafeFormat(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def command_message(command: str, language: str, key: str, **values: Any) -> str:
    """Return one editable message, safely falling back when needed."""
    messages = _load_messages()
    language = "ar" if language == "ar" else "en"
    command_data = messages.get(command) or messages.get("common") or {}
    localized = command_data.get(language) or command_data.get("en") or {}
    fallback_data = DEFAULT_COMMAND_MESSAGES.get(command, DEFAULT_COMMAND_MESSAGES["common"])
    fallback = fallback_data.get(language) or fallback_data.get("en") or {}
    template = localized.get(key) or fallback.get(key) or DEFAULT_COMMAND_MESSAGES["common"]["en"].get(key) or key
    try:
        return template.format_map(_SafeFormat(values))
    except (KeyError, ValueError):
        return template
