"""認証の差し込みポイント。

v1(ローカル利用)では常に匿名ユーザーを返す。
社内公開時は get_current_user を OIDC / LDAP / Basic 認証などの
実装に差し替えるだけで、全 API に認証がかかる。
"""

from dataclasses import dataclass


@dataclass
class User:
    id: str
    name: str


async def get_current_user() -> User:
    return User(id="anonymous", name="ローカルユーザー")
