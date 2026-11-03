"use client";

import Link from "next/link";
import {
  Suspense,
} from "react";
import {
  useRouter,
  useSearchParams,
} from "next/navigation";

import CustomerQuiz from "@/components/CustomerQuiz";
import {
  PageHeader,
} from "@/components/PageHeader";


function PersonalityQuizContent() {
  const router =
    useRouter();

  const searchParams =
    useSearchParams();

  const resetDraft =
    searchParams.get("mode") ===
    "retake";

  return (
    <div className="content-wrap personality-quiz-page">
      <PageHeader
        eyebrow="PERSONALITY QUIZ"
        title="消費人格測驗"
        description="依照平時購買保養品的習慣作答；完成後會顯示分析，登入會員即可同步到基本資料。"
      />

      <div className="personality-quiz-toolbar">
        <Link href="/">
          ← 返回首頁
        </Link>
      </div>

      <CustomerQuiz
        resetDraft={
          resetDraft
        }
        onReturnToProfile={() =>
          router.replace(
            "/profile",
          )
        }
      />

      <style jsx>{`
        .personality-quiz-page {
          padding-bottom: 48px;
        }

        .personality-quiz-toolbar {
          width: min(880px, 100%);
          margin: 0 auto 10px;
        }

        .personality-quiz-toolbar :global(a) {
          display: inline-flex;
          align-items: center;
          min-height: 44px;
          padding: 8px 14px;
          color: #76594d;
          font-size: 16px;
          font-weight: 700;
          text-decoration: none;
          border-radius: 999px;
        }

        .personality-quiz-toolbar :global(a:hover) {
          background: rgba(231, 215, 237, 0.58);
        }
      `}</style>
    </div>
  );
}


export default function PersonalityQuizPage() {
  return (
    <Suspense
      fallback={
        <main className="content-wrap">
          正在載入人格測驗…
        </main>
      }
    >
      <PersonalityQuizContent />
    </Suspense>
  );
}
