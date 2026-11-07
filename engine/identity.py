"""Identity resolution: current snapshot + historical changes -> every identifier of a user."""
import hashlib
from collections import defaultdict

from common import norm, read_csv


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Identities:
    def __init__(self):
        self.users = {}
        self.key_to_user = {}
        history = defaultdict(list)
        for row in read_csv("user_audit_log.csv"):
            history[row["user_id"]].append(row)
        for row in read_csv("user_identities.csv"):
            uid = row["user_id"]
            emails = {norm(row["email"])}
            phones = {row["phone"].strip()}
            hashes = {norm(row["email_hash"])}
            for change in history.get(uid, []):
                field, old = change["field_name"], change["old_value"]
                if field == "email":
                    emails.add(norm(old))
                elif field == "phone":
                    phones.add(old.strip())
                elif field == "email_hash":
                    hashes.add(norm(old))
            hashes |= {sha256(e) for e in emails}
            self.users[uid] = {
                "user_id": uid,
                "name": row["name"],
                "emails": sorted(e for e in emails if e),
                "phones": sorted(p for p in phones if p),
                "hashes": sorted(h for h in hashes if h),
                "history_audit_ids": [c["audit_id"] for c in history.get(uid, [])],
            }
            for key in self.keys(uid):
                self.key_to_user.setdefault(key, uid)

    def keys(self, uid):
        """Normalised identifier strings that link evidence to this user (names excluded on purpose)."""
        u = self.users[uid]
        keys = {uid.lower()} | set(u["emails"]) | set(u["hashes"])
        keys |= {_phone_key(p) for p in u["phones"]}
        return keys

    def resolve_email(self, email):
        return self.key_to_user.get(norm(email))

    def owner_of(self, value):
        """user_id owning an identifier value (email, hash, phone or user id), else None."""
        if value is None:
            return None
        key = norm(value)
        return self.key_to_user.get(key) or self.key_to_user.get(_phone_key(str(value)))


def _phone_key(phone):
    digits = "".join(ch for ch in phone if ch.isdigit())
    return "tel:" + digits if digits else None
