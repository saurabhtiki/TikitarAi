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
    FaqEntry,
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
    # Phase 61: the side questions the bot may answer, copied into the meeting's FAQ.
    faq: list[FaqEntry] = field(default_factory=list)


def _faq(*pairs: tuple[str, str]) -> list[FaqEntry]:
    return [FaqEntry(question=question, answer=answer) for question, answer in pairs]


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
        faq=_faq(
                ("Can you explain this role?", "This role is responsible for [key responsibilities] and requires experience in [skills/domain]. For more details Plz refer Job Description."),
                ("What is the interview process?", "The process may include an HR round, functional or technical evaluation, hiring-manager discussion, and final selection review."),
                ("What skills are you looking for?", "We assess job-specific capability, communication, problem-solving, teamwork, and relevant experience."),
                ("Is this a remote, hybrid, or office-based role?", "The work arrangement depends on the role, location, and company policy."),
                ("What is the expected salary range?", "Compensation depends on experience, role level, location, and internal pay structure."),
                ("What are the working hours?", "Working hours depend on the department, location, and business requirements."),
                ("What growth opportunities are available?", "Employees may have opportunities for training, performance-based growth, internal mobility, and leadership development."),
                ("How should I prepare for the interview?", "Review the job description and prepare examples of achievements, challenges handled, and measurable results."),
                ("Can I reschedule my interview?", "Yes. Please share your preferred date and time, and I will check available interview slots."),
                ("When will I receive feedback?", "We aim to provide an update within [X] business days, subject to interview completion and internal review."),
                ("Can I apply for more than one role?", "Yes, if your experience is relevant to multiple open roles."),
                ("What is the probation period?", " The probation period is based on company policy ,normally 3 to 6 months and will be communicated in the offer letter."),
                ("What is the company culture like?", "Our culture focuses on accountability, collaboration, learning, ethical conduct, and business results."),
                ("What documents are required?", " An updated resume is generally required. Qualification, identity, employment, and background verification documents may be requested later."),
                ("How company handle performance evaluations, and how often do they occur?","We conduct formal performance and salary reviews annually, supplemented by mid-year check-ins. Your manager will also hold bi-monthly 1-on-1s to ensure your goals stay aligned."),

        ),
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
        faq=_faq(
            ("How can I submit my quotation for this RFQ?", " Please submit your quotation through this Chat or email before the RFQ closing date, along with all requested documents."),
            ("What information must be included in the quotation?", "Include item description, specification compliance, quantity, unit price, taxes, delivery lead time, payment terms, validity, and warranty details."),
            ("What is the RFQ closing date and time?", "The submission deadline is stated in the RFQ. Quotations received after the deadline may not be considered."),
            ("Can I submit a quotation after the deadline?", "Late submissions are subject to procurement policy and may be accepted only with approved justification."),
            ("Can I offer an alternative product or specification?", "Yes. Clearly identify the alternative, provide technical specifications, explain deviations, and state any commercial impact."),
            ("Can I quote for only some RFQ items?", "Yes, unless the RFQ specifically requires a complete quotation. Clearly mention which items you are quoting for."),
            ("Can I revise my quotation?", "You may revise your quotation before the submission deadline. Please clearly mark it as a revised version."),
            ("What should be the quotation validity period?", "Please follow the validity period requested in the RFQ. If you cannot meet it, state your proposed validity period clearly."),
            ("Are taxes and freight included in the quoted price?", "Please specify whether prices are inclusive or exclusive of taxes, freight, insurance, packaging, installation, or other charges."),
            ("What delivery terms should I mention?", "State the delivery location, lead time, Incoterms if applicable, dispatch terms, and any conditions affecting delivery."),
            ("What payment terms are acceptable?", "Please quote your standard payment terms. Final terms are subject to company procurement policy and commercial approval."),
            ("Do I need to submit technical documents?", "Yes, where applicable. Include product catalogues, datasheets, compliance certificates, drawings, and warranty information."),
            ("How will suppliers be evaluated?", "Evaluation considers technical compliance, price, quality, delivery capability, commercial terms, past performance, and supplier eligibility."),
            ("Can I contact the team for clarification?", "Yes. Send clarification questions through the official RFQ communication channel before the clarification deadline."),
            ("When will the purchase decision be communicated?", "Selected suppliers will be notified after technical and commercial evaluation, approvals, and final procurement review."),

        ),
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
        faq=_faq(
            ("When will I get the quotation?", "Within 2 working days after this chat."),
            ("Is delivery included in the price?", "Delivery charges are shown separately in the quotation."),
            ("Can I get a sample?", "Yes, our sales team will arrange one after the quotation."),
        ),
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
        faq=_faq(
            ("Who sees my answers?", "Only the Project Manager running this review."),
            ("Can I change the deadline later?", "Yes, tell the Project Manager in the next weekly review."),
        ),
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
        faq=_faq(
            ("How can I pay?", "By bank transfer to the account printed on the invoice."),
            ("Can I get a copy of the invoice?", "Yes, our accounts team will email it to you after this chat."),
            ("Whom do I contact about a wrong invoice?", "Tell us here under Any disputes; accounts will call you."),
        ),
    ),
]

TEMPLATE_BY_NAME = {template.name: template for template in TEMPLATES}


def is_builtin_name(name: str) -> bool:
    """True for a ready-made template's name, in any case — a user's own can't reuse one."""
    return name.strip().lower() in {template.name.lower() for template in TEMPLATES}
