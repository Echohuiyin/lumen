import re


def extract_section(text: str, marker: str) -> str:
    pattern = rf"{re.escape(marker)}:\s*\n?(.*?)(?:\n[A-Z_]+:|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""


def parse_coordinator_response(text: str) -> dict:
    if "FINAL_RESPONSE:" in text:
        return {
            "mode": "summarize",
            "final_response": extract_section(text, "FINAL_RESPONSE"),
        }
    if "USER_QUESTION:" in text:
        return {
            "mode": "ask_user",
            "question": extract_section(text, "USER_QUESTION"),
        }
    return {
        "mode": "plan",
        "task_plan": extract_section(text, "TASK_PLAN") or text.strip(),
    }


def parse_executor_response(text: str) -> dict:
    if "STATUS: failed" in text:
        return {
            "status": "failed",
            "content": extract_section(text, "ERROR") or text.strip(),
        }
    return {
        "status": "success",
        "content": extract_section(text, "RESULT") or text.strip(),
    }


def parse_reviewer_response(text: str) -> dict:
    if "REVIEW: rejected" in text:
        return {
            "status": "rejected",
            "feedback": extract_section(text, "FEEDBACK") or text.strip(),
        }
    return {
        "status": "approved",
        "feedback": extract_section(text, "SUMMARY") or text.strip(),
    }
