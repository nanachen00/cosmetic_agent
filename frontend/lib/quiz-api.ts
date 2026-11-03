import { API_ORIGIN } from "@/lib/api-origin";

const API_BASE = API_ORIGIN;

function apiFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  return fetch(input, { ...init, credentials: "include" });
}


/* =========================================================
   Frontend Types
========================================================= */

export type QuizOption = {
  id: string;
  label: string;
  description?: string;
};


export type QuizQuestion = {
  id: string;
  question: string;
  options: QuizOption[];
};


export type QuizAnswerMap =
  Record<string, string>;


/* =========================================================
   Backend Question Types
========================================================= */

type BackendQuizAnswer = {
  answer_id: string;
  label: string;
  description?: string;
};


type BackendQuizQuestion = {
  question_id: string;
  question: string;
  answers: BackendQuizAnswer[];
};


type QuizQuestionsResponse = {
  ok: boolean;
  questions: BackendQuizQuestion[];
};


/* =========================================================
   Submit Response
========================================================= */

export type QuizSubmitResponse = {
  ok: boolean;
  persisted?: boolean;
  storage_scope?: "session" | "database";

  event_id?: string;
  event_mongo_id?: string;

  quiz_result_id?: string;
  quiz_result_mongo_id?: string;

  quiz_version?: string;

  primary_type?: string;
  secondary_type?: string;

  scores?: Record<string, number>;

  confidence?: number;
};


/* =========================================================
   Latest Quiz Result
========================================================= */

export type LatestQuizResult = {
  quiz_result_id?: string | null;

  quiz_version?: string | null;

  primary_type?: string | null;

  secondary_type?: string | null;

  scores?: Record<string, number>;

  confidence?: number;

  completed_at?: string | null;
};


export type LatestQuizResponse = {
  ok: boolean;

  has_completed: boolean;

  result:
    | LatestQuizResult
    | null;
};


/* =========================================================
   Error Helper
========================================================= */

async function getErrorMessage(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const body =
      await response.json();

    if (
      typeof body?.detail ===
      "string"
    ) {
      return fallback;
    }
  } catch {
    // ignore
  }

  return fallback;
}


/* =========================================================
   GET Quiz Questions

   GET /api/quiz/questions
========================================================= */

export async function getQuizQuestions():
Promise<QuizQuestion[]> {
  const response =
    await apiFetch(
      `${API_BASE}/api/quiz/questions`,
      {
        method:
          "GET",

        cache:
          "no-store",
      },
    );


  if (
    !response.ok
  ) {
    throw new Error(
      await getErrorMessage(
        response,
        `取得測驗題目失敗 (${response.status})`,
      ),
    );
  }


  const data:
    QuizQuestionsResponse =
    await response.json();


  if (
    !data.ok ||
    !Array.isArray(
      data.questions,
    )
  ) {
    throw new Error(
      "後端回傳的測驗題目格式不正確",
    );
  }


  /*
   * Backend
   *
   * question_id
   * answers
   * answer_id
   *
   * ↓
   *
   * Frontend
   *
   * id
   * options
   * id
   */

  return data.questions.map(
    (question) => ({
      id:
        question.question_id,

      question:
        question.question,

      options:
        question.answers.map(
          (answer) => ({
            id:
              answer.answer_id,

            label:
              answer.label,

            description:
              answer.description,
          }),
        ),
    }),
  );
}


/* =========================================================
   POST Quiz Answers

   POST /api/quiz/submit
========================================================= */

export async function submitQuiz(
  userId: string,
  selectedAnswers: QuizAnswerMap,
  sessionId?: string,
  messageId?: string,
): Promise<QuizSubmitResponse> {
  const response =
    await apiFetch(
      `${API_BASE}/api/quiz/submit`,
      {
        method:
          "POST",

        headers: {
          "Content-Type":
            "application/json",
        },

        body:
          JSON.stringify({
            user_id:
              userId,

            selected_answers:
              selectedAnswers,

            session_id:
              sessionId ??
              null,

            message_id:
              messageId ??
              null,
          }),
      },
    );


  if (
    !response.ok
  ) {
    throw new Error(
      await getErrorMessage(
        response,
        `送出測驗失敗 (${response.status})`,
      ),
    );
  }


  const data:
    QuizSubmitResponse =
    await response.json();


  if (
    !data.ok
  ) {
    throw new Error(
      "測驗結果儲存失敗",
    );
  }


  return data;
}


/* =========================================================
   GET Latest Quiz Result

   GET /api/quiz/latest?user_id=...
========================================================= */

export async function getLatestQuizResult(
  userId: string,
): Promise<LatestQuizResponse> {
  const params =
    new URLSearchParams({
      user_id:
        userId,
    });


  const response =
    await apiFetch(
      `${API_BASE}/api/quiz/latest?${params.toString()}`,
      {
        method:
          "GET",

        cache:
          "no-store",
      },
    );


  if (
    !response.ok
  ) {
    throw new Error(
      await getErrorMessage(
        response,
        `取得消費人格結果失敗 (${response.status})`,
      ),
    );
  }


  const data:
    LatestQuizResponse =
    await response.json();


  if (
    !data.ok
  ) {
    throw new Error(
      "取得消費人格結果失敗",
    );
  }


  return data;
}
