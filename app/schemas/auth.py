from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SignUpRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class TotpEnrollResponse(BaseModel):
    # provisioning_uri → QR; secret + recovery_codes shown ONCE (store them now).
    provisioning_uri: str
    secret: str
    recovery_codes: list[str]


class TotpVerifyRequest(BaseModel):
    # Accepts a 6-digit TOTP or a 16-char recovery code (step-up only).
    code: str = Field(min_length=6, max_length=64)


class TotpStatusResponse(BaseModel):
    enabled: bool


class StepUpResponse(BaseModel):
    step_up_token: str
    expires_in: int


class SignInRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_expires_in: int


class TwoFactorRequiredResponse(BaseModel):
    # Returned by /signin when the user has a second factor: no tokens yet — exchange
    # challenge_token + a code at /2fa/login. ``two_factor_required`` discriminates
    # this from TokenResponse on the shared /signin response. ``factors`` advertises
    # which second factors the user can use so the UI offers the right path(s); for
    # ``email`` the UI first calls /2fa/login/email/request to send a code.
    two_factor_required: Literal[True] = True
    challenge_token: str
    expires_in: int
    factors: list[str] = Field(default_factory=list)


class TwoFactorLoginEmailRequest(BaseModel):
    # Send an email_2fa_login code for the user behind a valid login challenge.
    challenge_token: str


class TwoFactorLoginRequest(BaseModel):
    challenge_token: str
    # ``totp`` (default; TOTP or recovery code) or ``email`` (an emailed login code).
    method: Literal["totp", "email"] = "totp"
    # Lowered to 4 to admit the email login code alongside 6-digit TOTP / recovery.
    code: str = Field(min_length=4, max_length=64)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=4096)


class EmailConfirmRequest(BaseModel):
    # T20 (W11c): which critical action the emailed code authorizes. Constrained to a
    # safe identifier charset (review) so it can't carry HTML into the email body.
    action: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9_]+$")


class EmailConfirmVerifyRequest(BaseModel):
    action: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9_]+$")
    code: str = Field(min_length=4, max_length=64)


class EmailConfirmStatusResponse(BaseModel):
    sent: bool
    enabled: bool


class EmailConfirmVerifyResponse(BaseModel):
    confirmed: bool


# ───────────────────────────── Email-2FA (factor) ──────────────────────────────


class Email2FACodeSentResponse(BaseModel):
    # A code was emailed (enroll / step-up / login). For enroll, the factor is not
    # active until /2fa/email/confirm.
    sent: bool


class Email2FAConfirmRequest(BaseModel):
    # The emailed enroll code (high-entropy token, 4–64 chars like email-confirm).
    code: str = Field(min_length=4, max_length=64)


class Email2FAStatusResponse(BaseModel):
    # ``enabled`` = a confirmed enrollment exists; ``available`` = Resend is
    # configured, so the user could enroll (the UI hides the card / shows 503 when not).
    enabled: bool
    available: bool


class StepUpRequest(BaseModel):
    # Factor-aware step-up: ``totp`` (default; a TOTP or recovery code) or ``email``
    # (an emailed ``email_2fa_step_up`` code requested via /2fa/step-up/email/request).
    method: Literal["totp", "email"] = "totp"
    code: str = Field(min_length=4, max_length=64)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    is_active: bool
    created_at: datetime
    updated_at: datetime


class AuthUserResponse(BaseModel):
    user: UserRead
    token: TokenResponse
