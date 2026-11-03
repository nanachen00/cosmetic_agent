import asyncio
import os
import unittest
from uuid import uuid4

from app.security.chat_guard import (
    ChatConcurrentRequest,
    ChatGuard,
    ChatRateLimitExceeded,
    DuplicateChatRequest,
    GuestSigner,
    estimate_tokens,
    hash_payload,
    valid_request_id,
)
from tools.getzep.conversation_memory_service import ConversationMemoryService


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


class MemoryChatGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.guard = ChatGuard(
            redis_url="",
            require_redis=False,
            key_prefix=f"test:{uuid4().hex}",
            account_limits=((2, 60),),
            guest_limits=((1, 60),),
            ip_limits=((10, 60),),
            account_daily_tokens=100,
            guest_daily_tokens=50,
            global_daily_requests=100,
            global_daily_tokens=1000,
            reserved_output_tokens=0,
            concurrency_lease_seconds=30,
            idempotency_ttl=600,
            clock=self.clock,
        )

    async def begin(self, request_id: str, **overrides):
        values = {
            "user_id": "account-a",
            "ip": "203.0.113.10",
            "request_id": request_id,
            "route": "/api/chat/message",
            "conversation_id": "conversation-a",
            "payload_hash": hash_payload("hello", None),
            "is_guest": False,
            "input_tokens": 5,
        }
        values.update(overrides)
        return await self.guard.begin(**values)

    async def test_conversation_creation_limits_account_and_ip(self):
        account_guard = ChatGuard(
            require_redis=False,
            key_prefix=f"test:{uuid4().hex}",
            conversation_create_account_limits=((2, 60),),
            conversation_create_ip_limits=((10, 60),),
            clock=self.clock,
        )
        await account_guard.check_conversation_create(
            user_id="account-a", ip="203.0.113.10"
        )
        await account_guard.check_conversation_create(
            user_id="account-a", ip="203.0.113.10"
        )
        with self.assertRaises(ChatRateLimitExceeded) as account_block:
            await account_guard.check_conversation_create(
                user_id="account-a", ip="203.0.113.10"
            )
        self.assertEqual(
            account_block.exception.scope, "conversation_create_account"
        )

        ip_guard = ChatGuard(
            require_redis=False,
            key_prefix=f"test:{uuid4().hex}",
            conversation_create_account_limits=((10, 60),),
            conversation_create_ip_limits=((2, 60),),
            clock=self.clock,
        )
        await ip_guard.check_conversation_create(
            user_id="account-a", ip="203.0.113.20"
        )
        await ip_guard.check_conversation_create(
            user_id="account-b", ip="203.0.113.20"
        )
        with self.assertRaises(ChatRateLimitExceeded) as ip_block:
            await ip_guard.check_conversation_create(
                user_id="account-c", ip="203.0.113.20"
            )
        self.assertEqual(ip_block.exception.scope, "conversation_create_ip")

    async def test_account_limit_and_window_reset(self):
        first = await self.begin("request-number-0001")
        await self.guard.finish(first)
        second = await self.begin("request-number-0002")
        await self.guard.finish(second)

        with self.assertRaises(ChatRateLimitExceeded) as caught:
            await self.begin("request-number-0003")
        self.assertEqual(caught.exception.scope, "account")
        self.assertGreaterEqual(caught.exception.retry_after, 1)

        self.clock.advance(61)
        after_reset = await self.begin("request-number-0004")
        await self.guard.finish(after_reset)

    async def test_guest_limit_is_stricter(self):
        guest = await self.begin(
            "guest-request-0001",
            user_id="guest-98e9cc5a-f439-45e8-9f4f-850242e5812f",
            is_guest=True,
        )
        await self.guard.finish(guest)
        with self.assertRaises(ChatRateLimitExceeded):
            await self.begin(
                "guest-request-0002",
                user_id="guest-98e9cc5a-f439-45e8-9f4f-850242e5812f",
                is_guest=True,
            )

    async def test_same_account_has_one_active_generation(self):
        first = await self.begin("request-number-0001")
        with self.assertRaises(ChatConcurrentRequest):
            await self.begin("request-number-0002", ip="198.51.100.20")

        other = await self.begin("request-number-0003", user_id="account-b")
        await self.guard.finish(first)
        await self.guard.finish(other)

        retried = await self.begin("request-number-0002", ip="198.51.100.20")
        await self.guard.finish(retried)

    async def test_idempotency_is_bound_to_payload_and_survives_finish(self):
        first = await self.begin("request-number-0001")
        await self.guard.finish(first)
        with self.assertRaises(DuplicateChatRequest) as same:
            await self.begin("request-number-0001")
        self.assertFalse(same.exception.different_payload)

        with self.assertRaises(DuplicateChatRequest) as changed:
            await self.begin(
                "request-number-0001",
                payload_hash=hash_payload("different", None),
            )
        self.assertTrue(changed.exception.different_payload)

    async def test_token_budget_is_hard_limit(self):
        expensive = await self.begin("request-number-0001", input_tokens=60)
        await self.guard.finish(expensive)
        with self.assertRaises(ChatRateLimitExceeded) as caught:
            await self.begin("request-number-0002", input_tokens=50)
        self.assertEqual(caught.exception.scope, "account_tokens")

    async def test_old_ticket_cannot_release_new_lock(self):
        first = await self.begin("request-number-0001")
        self.clock.advance(31)
        second = await self.begin("request-number-0002")
        await self.guard.finish(first)
        with self.assertRaises(ChatConcurrentRequest):
            await self.begin("request-number-0003")
        await self.guard.finish(second)


class GuestSignerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.signer = GuestSigner(
            secret="unit-test-secret-with-at-least-24-characters",
            ttl_seconds=300,
            clock=self.clock,
        )

    def test_issue_verify_tamper_and_expiry(self):
        guest_id, token = self.signer.issue()
        self.assertEqual(self.signer.verify(token), guest_id)
        replacement = "a" if token[-1] != "a" else "b"
        self.assertIsNone(self.signer.verify(token[:-1] + replacement))
        self.clock.advance(301)
        self.assertIsNone(self.signer.verify(token))

    def test_wrong_secret_cannot_verify(self):
        _, token = self.signer.issue()
        other = GuestSigner(
            secret="a-different-unit-test-secret-value",
            ttl_seconds=300,
            clock=self.clock,
        )
        self.assertIsNone(other.verify(token))

    def test_helpers(self):
        self.assertTrue(valid_request_id("client-message-123456"))
        self.assertFalse(valid_request_id("short"))
        self.assertGreaterEqual(estimate_tokens("皮膚狀況"), 4)
        self.assertEqual(hash_payload("a", "b"), hash_payload("a", "b"))


class _MemoryAdapter:
    user_id = "account-a"
    thread_id = "conversation-a"

    def to_summary(self):
        return ""

    def get_context_block(self):
        return ""

    def add_user_message(self, content):
        return None

    def add_assistant_message(self, content, metadata=None):
        return None


class ConversationMemoryBudgetTests(unittest.TestCase):
    def test_cjk_history_is_trimmed_by_token_budget(self):
        service = ConversationMemoryService(
            _MemoryAdapter(),
            max_context_chars=6000,
            max_context_tokens=512,
        )
        prompt = service.build_prompt(
            user_message="我的皮膚最近很乾",
            zep_context="歷史膚況" * 1500,
        )
        self.assertIn("……中間部分已省略……", prompt)
        self.assertNotIn("歷史膚況" * 1000, prompt)


@unittest.skipUnless(os.getenv("TEST_REDIS_URL"), "TEST_REDIS_URL is not configured")
class RedisChatGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_instances_share_counts_and_lock(self):
        prefix = f"test:blink:chat:{uuid4().hex}"
        kwargs = dict(
            redis_url=os.environ["TEST_REDIS_URL"],
            require_redis=True,
            key_prefix=prefix,
            account_limits=((1, 60),),
            guest_limits=((1, 60),),
            ip_limits=((10, 60),),
            account_daily_tokens=1000,
            guest_daily_tokens=1000,
            global_daily_requests=100,
            global_daily_tokens=10000,
            reserved_output_tokens=0,
        )
        first_guard = ChatGuard(**kwargs)
        second_guard = ChatGuard(**kwargs)
        ticket = await first_guard.begin(
            user_id="account-a",
            ip="203.0.113.10",
            request_id="request-number-0001",
            route="/api/chat/message",
            conversation_id="conversation-a",
            payload_hash=hash_payload("hello", None),
            is_guest=False,
            input_tokens=5,
        )
        with self.assertRaises(ChatConcurrentRequest):
            await second_guard.begin(
                user_id="account-a",
                ip="203.0.113.10",
                request_id="request-number-0002",
                route="/api/chat/message",
                conversation_id="conversation-a",
                payload_hash=hash_payload("hello again", None),
                is_guest=False,
                input_tokens=5,
            )
        await first_guard.finish(ticket)
        with self.assertRaises(ChatRateLimitExceeded):
            await second_guard.begin(
                user_id="account-a",
                ip="203.0.113.10",
                request_id="request-number-0002",
                route="/api/chat/message",
                conversation_id="conversation-a",
                payload_hash=hash_payload("hello again", None),
                is_guest=False,
                input_tokens=5,
            )


if __name__ == "__main__":
    unittest.main()
