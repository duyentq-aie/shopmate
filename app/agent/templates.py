"""The fixed replies of the agent: questions, refusals, hand-offs, small talk, the ticket texts and the step-cap message.

Only product, policy and order-status answers are written by the LLM (docs/DECISIONS-phase6.md, A3). Everything here is a
constant or a function of a few safe arguments, so a refusal can never leak anything. The two phrases of the relaxation rule
(A4) are exported by name because the evaluation scores against them.
"""

from __future__ import annotations

CONSTRAINT_LABELS = {"size": "kích cỡ", "color": "màu sắc", "material": "chất liệu"}

SMALLTALK = ("Chào bạn! Mình là trợ lý của ShopMate Fashion. Bạn cần tìm sản phẩm, hỏi về chính sách đổi trả, vận chuyển, bảo hành "
             "hay kiểm tra đơn hàng, cứ nhắn mình nhé.")
OUT_OF_SCOPE = ("Xin lỗi, mình chỉ hỗ trợ về sản phẩm, chính sách và đơn hàng của ShopMate Fashion nên chưa giúp được việc này. "
                "Bạn cần mình hỗ trợ gì về cửa hàng không?")
TECHNICAL_PROBLEM = "Xin lỗi, mình đang gặp trục trặc kỹ thuật nên chưa xử lý được tin nhắn này. Bạn thử gửi lại giúp mình nhé."
STEP_CAP = "Xin lỗi, mình chưa xử lý xong yêu cầu này. Bạn thử diễn đạt lại hoặc liên hệ nhân viên của ShopMate Fashion nhé."

ASK_PRODUCT_TYPE = ("Bạn đang tìm loại sản phẩm nào (ví dụ áo khoác, giày sneaker, túi xách)? "
                    "Cho mình biết thêm màu sắc, kích cỡ hoặc mức giá nếu có nhé.")
ASK_NUMBER_AND_EMAIL = "Để kiểm tra, bạn cho mình xin số đơn hàng và email đặt hàng nhé."
ASK_ORDER_NUMBER = "Bạn cho mình xin số đơn hàng nhé."
ASK_EMAIL = "Để bảo mật, mình cần email đặt hàng của đơn này trước khi cung cấp thông tin. Bạn cho mình xin email nhé."
ASK_EMAIL_FOR_PHONE = "Mình chưa xác minh đơn bằng số điện thoại được. Bạn cho mình xin email đặt hàng của đơn này nhé."
ASK_CONTACT_EMAIL = "Để nhân viên liên hệ lại với bạn, bạn cho mình xin địa chỉ email nhé."
HANDOFF = ("Mình chưa đủ thông tin để xử lý yêu cầu này. "
           "Bạn vui lòng liên hệ nhân viên của ShopMate Fashion để được hỗ trợ trực tiếp nhé.")

REFUSE_VERIFICATION = ("Mình không xác minh được đơn hàng với thông tin bạn cung cấp. "
                       "Bạn kiểm tra lại số đơn và email đặt hàng giúp mình nhé.")
REFUSE_VERIFICATION_LOCKED = (REFUSE_VERIFICATION + " Vì thông tin chưa khớp nhiều lần, mình tạm dừng tra cứu đơn hàng trong cuộc trò chuyện này. "
                              "Bạn vui lòng liên hệ nhân viên để được hỗ trợ.")
LOOKUP_DISABLED = ("Mình đã tạm dừng tra cứu đơn hàng trong cuộc trò chuyện này vì thông tin xác minh chưa khớp nhiều lần. "
                   "Bạn vui lòng liên hệ nhân viên để được hỗ trợ.")

# The two sentences of the relaxation rule (A4): scoring looks for them.
NO_FULL_MATCH = "Mình không tìm thấy sản phẩm nào khớp tất cả yêu cầu của bạn."
NO_RESULTS = NO_FULL_MATCH + " Bạn thử đổi bớt điều kiện hoặc mô tả khác giúp mình nhé."
NO_POLICY_INFO = ("Mình chưa có thông tin về nội dung này trong chính sách của cửa hàng nên chưa trả lời chính xác được. "
                  "Bạn vui lòng liên hệ nhân viên để được giải đáp nhé.")
# Said when a generated answer is rejected and the text named nothing the evidence can back (A21): it asserts nothing about any product or clause.
NO_INFORMATION = ("Mình chưa trả lời chính xác được câu hỏi này từ dữ liệu của cửa hàng. "
                  "Bạn vui lòng liên hệ nhân viên của ShopMate Fashion để được hỗ trợ nhé.")

TICKET_REASK = 'Mình chưa rõ bạn muốn tạo phiếu hay không. Bạn trả lời "có" để tạo hoặc "không" để hủy nhé.'
TICKET_CANCELLED = "Mình đã hủy, không tạo phiếu nào. Cần gì thêm bạn cứ nhắn mình nhé."
TICKET_NOT_CONFIRMED = "Vì mình chưa nhận được xác nhận rõ ràng nên không tạo phiếu. Khi cần, bạn cứ nhắn lại, mình sẽ hỗ trợ."
TICKET_FAILED = "Xin lỗi, mình chưa tạo được phiếu hỗ trợ do lỗi hệ thống. Bạn vui lòng liên hệ nhân viên trực tiếp nhé."


def relaxed_note(constraint: str) -> str:
    """Said right after NO_FULL_MATCH, before any suggestion: which constraint the suggestions leave out."""
    return f"Nếu bỏ điều kiện {CONSTRAINT_LABELS[constraint]}, mình có gợi ý sau:"


def no_results_relaxed(constraint: str) -> str:
    return f"{NO_FULL_MATCH} Mình cũng đã thử bỏ điều kiện {CONSTRAINT_LABELS[constraint]} nhưng vẫn không có kết quả."


def ticket_proposal(summary: str) -> str:
    return (f"Mình sẽ tạo phiếu hỗ trợ với nội dung sau:\n{summary}\n"
            'Bạn xác nhận tạo phiếu này không? (trả lời "có" để tạo, "không" để hủy)')


def ticket_created(ticket_id: str) -> str:
    return f"Mình đã tạo phiếu hỗ trợ {ticket_id}. Nhân viên sẽ liên hệ với bạn qua email."


# kind -> text, for the replies that are rendered by the `reply` node
_FIXED = {
    "smalltalk": SMALLTALK, "out_of_scope": OUT_OF_SCOPE, "technical_problem": TECHNICAL_PROBLEM, "step_cap": STEP_CAP,
    "ask_product_type": ASK_PRODUCT_TYPE, "ask_number_and_email": ASK_NUMBER_AND_EMAIL, "ask_order_number": ASK_ORDER_NUMBER,
    "ask_email": ASK_EMAIL, "ask_email_for_phone": ASK_EMAIL_FOR_PHONE, "ask_contact_email": ASK_CONTACT_EMAIL, "handoff": HANDOFF,
    "refuse_verification": REFUSE_VERIFICATION, "refuse_verification_locked": REFUSE_VERIFICATION_LOCKED, "lookup_disabled": LOOKUP_DISABLED,
    "no_results": NO_RESULTS, "no_policy_info": NO_POLICY_INFO,
    "ticket_cancelled": TICKET_CANCELLED, "ticket_not_confirmed": TICKET_NOT_CONFIRMED, "ticket_failed": TICKET_FAILED,
}
# what the agent is waiting for after each question; every other kind ends the request
AWAITING = {"ask_product_type": "product_type", "ask_number_and_email": "order_number_and_email", "ask_order_number": "order_number",
            "ask_email": "email", "ask_email_for_phone": "email", "ask_contact_email": "email"}


def render(kind: str, **args) -> str:
    if kind in _FIXED:
        return _FIXED[kind]
    if kind == "ticket_created":
        return ticket_created(args["ticket_id"])
    raise KeyError(f"unknown reply kind {kind!r}")
