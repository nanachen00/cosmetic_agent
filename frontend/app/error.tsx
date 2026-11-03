'use client';

import * as Sentry from '@sentry/nextjs';
import { useEffect } from 'react';

type ErrorBoundaryProps = {
  error: Error & { digest?: string };
  reset: () => void;
};

export default function ErrorBoundary({ error, reset }: ErrorBoundaryProps) {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);

  return (
    <main style={{ minHeight: '70vh', display: 'grid', placeItems: 'center', padding: 24 }}>
      <section style={{ width: 'min(520px, 100%)', padding: 32, borderRadius: 20, background: '#fff', color: '#17233d', boxShadow: '0 18px 60px rgba(35, 64, 55, .12)' }}>
        <p style={{ margin: '0 0 8px', color: '#1c7b5b', fontSize: 12, fontWeight: 800, letterSpacing: '.14em' }}>
          BLINK AI
        </p>
        <h1 style={{ margin: 0, fontSize: 30 }}>這個頁面暫時無法顯示</h1>
        <p style={{ margin: '14px 0 24px', color: '#647084', lineHeight: 1.7 }}>
          錯誤已自動回報給技術團隊，聊天內容不會包含在回報中。
        </p>
        <button
          type="button"
          onClick={reset}
          style={{ minHeight: 44, border: 0, borderRadius: 12, padding: '0 18px', background: '#173d32', color: '#fff', font: 'inherit', fontWeight: 750, cursor: 'pointer' }}
        >
          再試一次
        </button>
      </section>
    </main>
  );
}
