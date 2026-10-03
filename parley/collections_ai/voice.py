"""The voice agent's prompt and tools (spec: "Tools on a call").

The model on a call can only talk and ask for these tools. Each one is run by
code in the services layer (`parley/services/calls.py`), which checks every
field before anything is saved. None of them can edit an invoice, record a
payment or change a setting.
"""

from parley.collections_ai.prompts import load_prompt
from parley.ports.voice import ToolSpec

VOICE_PROMPT = "voice_agent.v1"

LANGUAGE_INSTRUCTIONS = {
    "hi": "Hindi",
    "hinglish": "Hindi mixed with English, as the customer does",
    "en": "Indian English",
}


def voice_system_prompt(business_name: str, customer_name: str, language: str) -> str:
    """Business and customer names only: amounts come from get_invoice, after the
    customer's identity is confirmed."""
    template = load_prompt(VOICE_PROMPT).text
    return template.format(
        business_name=business_name,
        customer_name=customer_name,
        customer_name_hint=f"{customer_name} (not yet confirmed)",
        # Nova 2 Sonic does not speak Marathi; Marathi speakers get Hindi.
        language_instruction=LANGUAGE_INSTRUCTIONS.get(
            language, "Indian English, switching to Hindi if the customer does"
        ),
    )


_INVOICES = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Invoice numbers this is about; leave empty for all of them.",
}

VOICE_TOOLS = [
    ToolSpec(
        "confirm_identity",
        "Call once the person confirms they are the customer, or handle the customer's payments.",
        {
            "type": "object",
            "properties": {
                "spoke_with": {"type": "string", "description": "The name they gave."},
                "confirmed": {"type": "boolean"},
            },
            "required": ["confirmed"],
        },
    ),
    ToolSpec(
        "get_invoice",
        "The customer's overdue invoices on this call, with amounts and due dates. "
        "Works only after confirm_identity.",
        {"type": "object", "properties": {}},
    ),
    ToolSpec(
        "log_promise",
        "Record the customer's promise to pay. Read the answer: only recorded=true "
        "means the promise counts.",
        {
            "type": "object",
            "properties": {
                "promised_date": {
                    "type": "string",
                    "description": "The date they will pay, as YYYY-MM-DD.",
                },
                "amount": {
                    "type": "string",
                    "description": "The amount, as a plain number, if not the full amount.",
                },
                "invoice_numbers": _INVOICES,
            },
            "required": ["promised_date"],
        },
    ),
    ToolSpec(
        "log_dispute",
        "Record that the customer disputes an invoice or says they already paid.",
        {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Their reason, in their words."},
                "already_paid": {"type": "boolean"},
                "invoice_numbers": _INVOICES,
            },
            "required": ["reason"],
        },
    ),
    ToolSpec(
        "send_payment_link",
        "Email the customer the business's payment link.",
        {"type": "object", "properties": {}},
    ),
    ToolSpec(
        "transfer_to_human",
        "Hand the customer to a person.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    ),
    ToolSpec(
        "end_call",
        "Hang up after saying goodbye.",
        {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["done", "answering_machine", "wrong_person", "customer_request"],
                }
            },
            "required": ["reason"],
        },
    ),
]
