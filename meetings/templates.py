"""Ready-made meetings to start from instead of a blank grid (phase 54).

Plain data: the New meeting window copies one into its boxes and grid, and the organiser
edits from there. Nothing here is stored — a meeting made from a template is an ordinary
meeting, so changing a template later never touches meetings already sent out.
"""

from dataclasses import dataclass, field

from meetings.model import (
    ANSWER_CHOICE,
    ANSWER_DATE,
    ANSWER_NUMBER,
    ANSWER_TEXT,
    ANSWER_YES_NO,
    QUESTION_ITEM,
    AgendaItem,
    EvaluationField,
)


@dataclass(frozen=True)
class MeetingTemplate:
    name: str
    description: str
    meeting_context: str
    persona: str
    context_sop: str
    agenda: list[AgendaItem] = field(default_factory=list)
    evaluation: list[EvaluationField] = field(default_factory=list)


def _question(title: str, answer_type: str, rule: str = "", branch: str = "", loop: str = "", note: str = "") -> AgendaItem:
    return AgendaItem(
        item=title, ai_note=note, item_type=QUESTION_ITEM, answer_type=answer_type, rule=rule, branch=branch, loop=loop
    )


TEMPLATES = [
    MeetingTemplate(
        name="HR interview",
        description="Salary, notice period, joining date and relocation, then a short chat about experience.",
        meeting_context="First-round screening for an open position. Collect the basics before the panel interview.",
        persona="You are the HR recruiter. Friendly, clear and brief.",
        context_sop="Do not promise a salary or an offer. The budget for this role is up to 150000 a month.",
        agenda=[
            _question("Expected salary", ANSWER_NUMBER, "between 10000 and 500000", "if > 150000 go to End",
                      note="Monthly salary in rupees."),
            _question("Notice period", ANSWER_NUMBER, "between 0 and 180", note="In days."),
            _question("Joining date", ANSWER_DATE, "future"),
            _question("Willing to relocate", ANSWER_YES_NO),
            AgendaItem(item="Experience", ai_note="Ask about their last two roles and main achievements."),
        ],
        evaluation=[
            EvaluationField(question="Years of experience", buckets=["Under 3", "3 to 7", "Over 7"]),
            EvaluationField(question="Communication", buckets=["Good", "Average", "Poor"]),
        ],
    ),
    MeetingTemplate(
        name="Vendor purchase",
        description="Price, delivery date, payment terms and warranty from a supplier.",
        meeting_context="Collect a firm offer from the vendor for this purchase order.",
        persona="You are the Purchase Manager. Polite but firm.",
        context_sop="Never agree to a price or terms yourself — only collect the vendor's offer.",
        agenda=[
            _question("Unit price", ANSWER_NUMBER, "> 0", note="Price per unit in rupees, before tax."),
            _question("Delivery date", ANSWER_DATE, "future"),
            _question("Payment terms", ANSWER_CHOICE, "Advance, 30 days, 60 days, 90 days"),
            _question("Warranty in months", ANSWER_NUMBER, "between 0 and 60"),
            AgendaItem(item="Quality and past issues", ai_note="Ask how they handle rejected material."),
        ],
        evaluation=[EvaluationField(question="Delivery confidence", buckets=["High", "Medium", "Low"])],
    ),
    MeetingTemplate(
        name="Sales quotation",
        description="Quantity, target price, delivery need and decision date from a customer.",
        meeting_context="Understand the customer's requirement so we can send a quotation.",
        persona="You are the Sales Executive. Helpful and to the point.",
        context_sop="Do not quote a price. Only collect what the customer needs.",
        agenda=[
            _question("Quantity", ANSWER_NUMBER, "> 0"),
            _question("Target price per unit", ANSWER_NUMBER, "> 0"),
            _question("Delivery needed by", ANSWER_DATE, "future"),
            _question("Other quotes received", ANSWER_YES_NO, branch="if No go to Decision date"),
            _question("Best competing price", ANSWER_NUMBER, "> 0"),
            _question("Decision date", ANSWER_DATE, "within 90 days"),
        ],
    ),
    MeetingTemplate(
        name="Project review",
        description="Is the task done? If not: why, new date and who is responsible.",
        meeting_context="Weekly review of the task assigned to this person.",
        persona="You are the Project Manager. Supportive but specific about dates.",
        context_sop="Accept only a real date for a new deadline, not 'soon'.",
        agenda=[
            _question("Task complete", ANSWER_YES_NO, branch="if Yes go to Next steps"),
            _question("Reason for delay", ANSWER_TEXT, "at least 10 characters"),
            _question("New completion date", ANSWER_DATE, "future"),
            _question("Responsible person", ANSWER_TEXT),
            _question("Next steps", ANSWER_TEXT),
        ],
    ),
    MeetingTemplate(
        name="AR review",
        description="For every outstanding invoice: paid on time? If not, expected date and reason.",
        meeting_context="Weekly follow-up on this customer's outstanding invoices.",
        persona="You are the Accounts Receivable executive. Courteous and firm about dates.",
        context_sop="Do not offer discounts or waive interest. Upload the Outstanding invoices list in "
        "Overview after creating the meeting.",
        agenda=[
            _question("Will it be paid on time", ANSWER_YES_NO, branch="if Yes go to Next row",
                      loop="Outstanding invoices"),
            _question("Expected payment date", ANSWER_DATE, "future", loop="Outstanding invoices"),
            _question("Reason for delay", ANSWER_TEXT, loop="Outstanding invoices"),
            AgendaItem(item="Any disputes", ai_note="Ask if any invoice is disputed and why."),
        ],
    ),
]

TEMPLATE_BY_NAME = {template.name: template for template in TEMPLATES}
