"use client";

import Link from "next/link";

import {
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  getQuizQuestions,
  submitQuiz,
  type QuizQuestion,
  type QuizSubmitResponse,
} from "@/lib/quiz-api";

import {
  getOrCreateCheckinUserId,
} from "@/lib/checkin-api";

import {
  bindUnsavedChangesGuard,
  readSession,
  removeSession,
  writeSession,
} from "@/lib/storage";


/* =========================================================
   Customer Type 中文顯示名稱
========================================================= */

const customerTypeLabels:
  Record<string, string> = {

  hesitant_decision_maker:
    "猶豫決策型",

  ingredient_researcher:
    "成分研究型",

  price_sensitive:
    "價格敏感型",

  review_dependent:
    "評價依賴型",

  brand_oriented:
    "品牌導向型",

  explorer:
    "探索嘗鮮型",

  function_oriented:
    "功效導向型",

  impulse_buyer:
    "衝動購買型",
};


/* =========================================================
   Customer Type Label Helper
========================================================= */

function getCustomerTypeLabel(
  type:
    | string
    | undefined
    | null,
): string {
  if (!type) {
    return "";
  }

  return (
    customerTypeLabels[
      type
    ] ??
    type
  );
}


/* =========================================================
   Props

   CustomerQuiz 完成之後，
   通知 Chat Page。

   true:
   已完成測驗並取得分析結果

   false:
   正在測驗 / 重新作答
========================================================= */

interface CustomerQuizProps {
  onCompletedChange?: (
    completed: boolean,
  ) => void;

  onReturnToProfile?: () => void;

  resetDraft?: boolean;
}


/* =========================================================
   Customer Quiz
========================================================= */

type CustomerQuizDraft = {
  currentIndex: number;
  answers: Record<string, string>;
  selectedOptionId: string | null;
  finished: boolean;
};


export default function CustomerQuiz({
  onCompletedChange,
  onReturnToProfile,
  resetDraft = false,
}: CustomerQuizProps) {
  const [
    questions,
    setQuestions,
  ] =
    useState<QuizQuestion[]>(
      [],
    );


  const [
    currentIndex,
    setCurrentIndex,
  ] =
    useState(0);


  const [
    answers,
    setAnswers,
  ] =
    useState<
      Record<string, string>
    >({});


  const [
    selectedOptionId,
    setSelectedOptionId,
  ] =
    useState<
      string | null
    >(null);


  const [
    finished,
    setFinished,
  ] =
    useState(false);


  const [
    loading,
    setLoading,
  ] =
    useState(true);


  const [
    submitting,
    setSubmitting,
  ] =
    useState(false);


  const [
    error,
    setError,
  ] =
    useState("");


  const [
    result,
    setResult,
  ] =
    useState<
      QuizSubmitResponse | null
    >(null);


  const [
    userId,
    setUserId,
  ] =
    useState("");

  const [
    loadAttempt,
    setLoadAttempt,
  ] =
    useState(0);


  /* =======================================================
     Load Quiz Questions
  ======================================================= */

  useEffect(() => {
    let cancelled =
      false;


    async function loadQuiz() {
      try {
        setLoading(
          true,
        );

        setError(
          "",
        );


        const uid =
          await getOrCreateCheckinUserId();


        setUserId(
          uid,
        );


        const quizQuestions =
          await getQuizQuestions();


        if (
          !cancelled
        ) {
          const draftKey =
            `blink-customer-quiz-draft:${uid}`;

          const draft =
            resetDraft
              ? null
              : readSession<CustomerQuizDraft | null>(
                  draftKey,
                  null,
                );

          if (
            resetDraft
          ) {
            removeSession(
              draftKey,
            );
          }

          /*
           * 問卷版本更新時，瀏覽器可能仍保存上一版的三題草稿。
           * 只保留目前題目與選項都仍然存在的答案，避免把舊 answer_id
           * 送到新版後端，造成「缺少答案／答案無效」的錯誤。
           */
          const validAnswers =
            draft
              ? quizQuestions.reduce<
                  Record<string, string>
                >(
                  (
                    sanitizedAnswers,
                    question,
                  ) => {
                    const answerId =
                      draft.answers[
                        question.id
                      ];

                    if (
                      answerId &&
                      question.options.some(
                        (option) =>
                          option.id ===
                          answerId,
                      )
                    ) {
                      sanitizedAnswers[
                        question.id
                      ] = answerId;
                    }

                    return sanitizedAnswers;
                  },
                  {},
                )
              : {};

          const hasValidAnswers =
            Object.keys(
              validAnswers,
            ).length > 0;

          const safeIndex =
            draft &&
            hasValidAnswers
              ? Math.min(
                  Math.max(
                    draft.currentIndex,
                    0,
                  ),
                  Math.max(
                    quizQuestions.length - 1,
                    0,
                  ),
                )
              : 0;

          const activeQuestion =
            quizQuestions[
              safeIndex
            ];

          setQuestions(
            quizQuestions,
          );

          if (
            draft
          ) {
            setCurrentIndex(
              safeIndex,
            );

            setAnswers(
              validAnswers,
            );

            setSelectedOptionId(
              activeQuestion
                ? validAnswers[
                    activeQuestion.id
                  ] ??
                  null
                : null,
            );

            setFinished(
              draft.finished &&
                quizQuestions.length > 0 &&
                quizQuestions.every(
                  (question) =>
                    Boolean(
                      validAnswers[
                        question.id
                      ],
                    ),
                ),
            );
          }
        }
      } catch (
        error
      ) {
        console.error(
          "Failed to load quiz questions:",
          error,
        );


        if (
          !cancelled
        ) {
          setError(
            error instanceof Error
              ? error.message
              : "無法取得測驗題目",
          );
        }
      } finally {
        if (
          !cancelled
        ) {
          setLoading(
            false,
          );
        }
      }
    }


    void loadQuiz();


    return () => {
      cancelled =
        true;
    };
  }, [
    loadAttempt,
    resetDraft,
  ]);

  const hasDraftProgress =
    useMemo(
      () =>
        finished ||
        currentIndex >
          0 ||
        selectedOptionId !==
          null ||
        Object.keys(
          answers,
        ).length >
          0,
      [
        answers,
        currentIndex,
        finished,
        selectedOptionId,
      ],
    );

  useEffect(() => {
    if (
      !userId ||
      loading ||
      result
    ) {
      return;
    }

    const draftKey =
      `blink-customer-quiz-draft:${userId}`;

    if (
      hasDraftProgress
    ) {
      writeSession<CustomerQuizDraft>(
        draftKey,
        {
          currentIndex,
          answers,
          selectedOptionId,
          finished,
        },
      );
    } else {
      removeSession(
        draftKey,
      );
    }
  }, [
    answers,
    currentIndex,
    finished,
    hasDraftProgress,
    loading,
    result,
    selectedOptionId,
    userId,
  ]);

  useEffect(() => {
    if (
      !hasDraftProgress ||
      result
    ) {
      return;
    }

    return bindUnsavedChangesGuard(
      "人格測驗尚未完成，確定要離開嗎？作答進度會保留。",
    );
  }, [
    hasDraftProgress,
    result,
  ]);


  /* =======================================================
     Current Question
  ======================================================= */

  const currentQuestion =
    questions[
      currentIndex
    ];


  /* =======================================================
     Progress
  ======================================================= */

  const progress =
    useMemo(() => {
      if (
        questions.length ===
        0
      ) {
        return 0;
      }


      return (
        (
          currentIndex +
          1
        ) /
        questions.length
      ) * 100;
    }, [
      currentIndex,
      questions.length,
    ]);


  /* =======================================================
     Select Option
  ======================================================= */

  const handleSelectOption =
    (
      optionId:
        string,
    ) => {
      setSelectedOptionId(
        optionId,
      );
    };


  /* =======================================================
     Next
  ======================================================= */

  const handleNext =
    () => {
      if (
        !selectedOptionId ||
        !currentQuestion
      ) {
        return;
      }


      const updatedAnswers = {
        ...answers,

        [currentQuestion.id]:
          selectedOptionId,
      };


      setAnswers(
        updatedAnswers,
      );


      /*
       * 最後一題
       */

      if (
        currentIndex ===
        questions.length -
          1
      ) {
        setFinished(
          true,
        );

        return;
      }


      /*
       * 下一題
       */

      const nextIndex =
        currentIndex +
        1;


      setCurrentIndex(
        nextIndex,
      );


      const nextQuestion =
        questions[
          nextIndex
        ];


      setSelectedOptionId(
        nextQuestion
          ? updatedAnswers[
              nextQuestion.id
            ] ??
            null
          : null,
      );
    };


  /* =======================================================
     Back
  ======================================================= */

  const handleBack =
    () => {
      if (
        currentIndex ===
        0
      ) {
        return;
      }


      const previousIndex =
        currentIndex -
        1;


      const previousQuestion =
        questions[
          previousIndex
        ];


      setCurrentIndex(
        previousIndex,
      );


      setSelectedOptionId(
        answers[
          previousQuestion.id
        ] ??
        null,
      );
    };


  /* =======================================================
     Restart

     重新作答時，
     告訴 Chat Page：
     現在已經不是 completed 狀態。
  ======================================================= */

  const handleRestart =
    () => {
      setCurrentIndex(
        0,
      );


      setAnswers(
        {},
      );


      setSelectedOptionId(
        null,
      );


      setFinished(
        false,
      );


      setResult(
        null,
      );


      setError(
        "",
      );

      if (
        userId
      ) {
        removeSession(
          `blink-customer-quiz-draft:${userId}`,
        );
      }


      onCompletedChange?.(
        false,
      );
    };


  /* =======================================================
     Submit Quiz

     POST /api/quiz/submit
  ======================================================= */

  async function handleSubmitQuiz() {
    if (
      !userId ||
      submitting
    ) {
      return;
    }


    try {
      setSubmitting(
        true,
      );


      setError(
        "",
      );


      const response =
        await submitQuiz(
          userId,
          answers,
        );


      setResult(
        response,
      );

      removeSession(
        `blink-customer-quiz-draft:${userId}`,
      );


      /*
       * 已經：
       *
       * 1. 完成所有題目
       * 2. POST 成功
       * 3. 拿到消費人格結果
       *
       * 此時通知 Chat Page。
       */

      onCompletedChange?.(
        true,
      );
    } catch (
      error
    ) {
      console.error(
        "Quiz submit failed:",
        error,
      );


      setError(
        error instanceof Error
          ? error.message
          : "測驗送出失敗",
      );
    } finally {
      setSubmitting(
        false,
      );
    }
  }


  /* =======================================================
     Loading
  ======================================================= */

  if (
    loading
  ) {
    return (
      <section className="customer-quiz-state-card">

        正在載入測驗題目...


        <style jsx>{`
          .customer-quiz-state-card {
            width: min(
              880px,
              100%
            );

            min-height:
              440px;

            margin:
              18px auto
              32px;

            padding:
              34px;

            display:
              flex;

            align-items:
              center;

            justify-content:
              center;

            text-align:
              center;

            border-radius:
              30px;

            background:
              rgba(
                255,
                255,
                255,
                0.94
              );

            border:
              3px solid
              #ffffff;

            outline:
              2px solid
              rgba(
                168,
                150,
                193,
                0.55
              );

            color:
              #806c72;
          }
        `}</style>

      </section>
    );
  }


  /* =======================================================
     Load Error
  ======================================================= */

  if (
    error &&
    questions.length ===
      0
  ) {
    return (
      <section className="customer-quiz-state-card">

        <h2>
          測驗暫時無法載入
        </h2>


        <p>
          {error}
        </p>

        <button
          type="button"
          onClick={() =>
            setLoadAttempt(
              (
                current,
              ) =>
                current +
                1,
            )
          }
        >
          重新載入題目
        </button>


        <style jsx>{`
          .customer-quiz-state-card {
            width: min(
              880px,
              100%
            );

            min-height:
              440px;

            margin:
              18px auto
              32px;

            padding:
              34px;

            display:
              flex;

            flex-direction:
              column;

            align-items:
              center;

            justify-content:
              center;

            text-align:
              center;

            border-radius:
              30px;

            background:
              rgba(
                255,
                255,
                255,
                0.94
              );

            border:
              3px solid
              #ffffff;

            outline:
              2px solid
              rgba(
                168,
                150,
                193,
                0.55
              );
          }

          h2 {
            margin:
              0 0 12px;

            color:
              #60464c;
          }

          p {
            color:
              #a75f68;
          }
        `}</style>

      </section>
    );
  }


  if (
    !currentQuestion
  ) {
    return null;
  }


  /* =======================================================
     Finished
  ======================================================= */

  if (
    finished
  ) {
    return (
      <section className="customer-quiz-card">

        <div className="customer-quiz-finish">

          <div className="customer-quiz-finish-icon">
            ♡
          </div>


          <div className="customer-quiz-finish-tag">
            QUIZ COMPLETE
          </div>


          {!result ? (
            <>

              <h2>
                測驗完成啦！
              </h2>


              <p>
                已經收到你的回答～
                <br />

                接下來會根據你的選擇分析消費人格，
                幫助 BLINK AI 更了解你的購物偏好。
              </p>


              <button
                type="button"
                className="customer-quiz-primary-button"
                onClick={
                  handleSubmitQuiz
                }
                disabled={
                  submitting
                }
              >
                {submitting
                  ? "分析中..."
                  : "查看我的消費人格"}
              </button>

            </>
          ) : (
            <>

              <h2>
                我的消費人格
              </h2>


              <div className="customer-quiz-result">

                <div className="customer-quiz-result-tag">
                  YOUR RESULT
                </div>


                <h3>
                  {result.primary_type
                    ? getCustomerTypeLabel(
                        result.primary_type,
                      )
                    : "分析完成 ♡"}
                </h3>


                {result.secondary_type ? (
                  <p>
                    次要消費人格：

                    <strong>
                      {" "}
                      {getCustomerTypeLabel(
                        result.secondary_type,
                      )}
                    </strong>
                  </p>
                ) : null}


                {typeof result.confidence ===
                "number" ? (
                  <p>
                    分析可信度：

                    <strong>
                      {" "}
                      {Math.round(
                        result.confidence *
                          100,
                      )}
                      %
                    </strong>
                  </p>
                ) : null}


                <div className="customer-quiz-result-note">
                  {result.persisted === false ? (
                    <>
                      這是本次瀏覽的訪客結果，尚未寫入會員資料。
                      <Link href="/login" className="customer-quiz-login-link">
                        登入後即可永久保存
                      </Link>
                    </>
                  ) : (
                    <>
                      BLINK AI 已經記錄這次測驗結果，後續會搭配你的實際瀏覽、
                      比較及購買行為持續更新偏好。
                    </>
                  )}
                </div>

              </div>


              {onReturnToProfile ? (
                <button
                  type="button"
                  className="customer-quiz-primary-button"
                  onClick={() => {
                    if (result.persisted === false) {
                      window.location.assign("/");
                      return;
                    }
                    onReturnToProfile();
                  }}
                >
                  {result.persisted === false
                    ? "完成並返回首頁"
                    : "完成並返回基本資料"}
                </button>
              ) : null}

            </>
          )}


          {error ? (
            <p className="customer-quiz-error">
              {error}
            </p>
          ) : null}


          <button
            type="button"
            className="customer-quiz-text-button"
            onClick={
              handleRestart
            }
          >
            重新作答
          </button>

        </div>


        <style jsx>{`
          .customer-quiz-card {
            width: min(
              880px,
              100%
            );

            margin:
              18px auto
              32px;

            padding:
              34px;

            background:
              linear-gradient(
                145deg,
                rgba(
                  255,
                  255,
                  255,
                  0.96
                ),
                rgba(
                  249,
                  246,
                  255,
                  0.9
                )
              );

            border:
              3px solid
              #ffffff;

            outline:
              2px solid
              rgba(
                168,
                150,
                193,
                0.55
              );

            border-radius:
              30px;

            box-shadow:
              0 10px 0
              rgba(
                111,
                82,
                86,
                0.12
              ),
              0 18px 34px
              rgba(
                80,
                65,
                95,
                0.1
              );
          }


          .customer-quiz-finish {
            min-height:
              360px;

            display:
              flex;

            flex-direction:
              column;

            align-items:
              center;

            justify-content:
              center;

            text-align:
              center;
          }


          .customer-quiz-finish-icon {
            width:
              76px;

            height:
              76px;

            margin-bottom:
              18px;

            display:
              flex;

            align-items:
              center;

            justify-content:
              center;

            border-radius:
              50%;

            background:
              #e8f7f3;

            border:
              2px dashed
              #8ebbb1;

            font-size:
              38px;

            color:
              #73545c;
          }


          .customer-quiz-finish-tag,
          .customer-quiz-result-tag {
            padding:
              7px 14px;

            margin-bottom:
              12px;

            background:
              #eee5f8;

            color:
              #725f84;

            border-radius:
              999px;

            font-size:
              11px;

            font-weight:
              800;

            letter-spacing:
              1px;
          }


          .customer-quiz-finish h2 {
            margin:
              0 0 12px;

            color:
              #60464c;

            font-size:
              28px;
          }


          .customer-quiz-finish > p {
            margin:
              0 0 28px;

            color:
              #806c72;

            line-height:
              1.8;

            font-size:
              14px;
          }


          .customer-quiz-primary-button {
            min-width:
              220px;

            border: 0;

            border-radius:
              999px;

            padding:
              13px 24px;

            background:
              linear-gradient(
                180deg,
                #80618e
                0%,
                #674b73
                100%
              );

            color:
              white;

            font-size:
              14px;

            font-weight:
              800;

            cursor:
              pointer;

            box-shadow:
              inset
              0 1px 0
              rgba(
                255,
                255,
                255,
                0.45
              ),
              0 5px 0
              rgba(
                72,
                50,
                82,
                0.18
              );
          }


          .customer-quiz-primary-button:disabled {
            opacity:
              0.55;

            cursor:
              not-allowed;
          }


          .customer-quiz-text-button {
            margin-top:
              14px;

            border:
              0;

            background:
              transparent;

            color:
              #806d77;

            font-size:
              13px;

            font-weight:
              700;

            cursor:
              pointer;

            text-decoration:
              underline;

            text-underline-offset:
              3px;
          }


          .customer-quiz-result {
            width:
              min(
                430px,
                100%
              );

            padding:
              24px;

            margin:
              10px 0
              6px;

            border-radius:
              22px;

            background:
              linear-gradient(
                145deg,
                #effaf7,
                #f7f0fb
              );

            border:
              1.5px solid
              rgba(
                143,
                188,
                177,
                0.4
              );
          }


          .customer-quiz-result h3 {
            margin:
              5px 0
              15px;

            color:
              #60464c;

            font-size:
              22px;
          }


          .customer-quiz-result p {
            margin:
              6px 0;

            color:
              #806c72;

            font-size:
              13px;
          }


          .customer-quiz-result-note {
            margin-top:
              18px;

            padding-top:
              15px;

            border-top:
              1px dashed
              rgba(
                128,
                97,
                142,
                0.28
              );

            color:
              #8c7a82;

            font-size:
              12px;

            line-height:
              1.7;
          }


          .customer-quiz-error {
            margin-top:
              15px !important;

            margin-bottom:
              0 !important;

            color:
              #a75f68 !important;
          }
        `}</style>

      </section>
    );
  }


  /* =======================================================
     Quiz UI
  ======================================================= */

  return (
    <section className="customer-quiz-card">

      <header className="customer-quiz-header">

        <div>

          <div className="customer-quiz-eyebrow">
            BLINK AI
          </div>


          <h2>
            消費人格小測驗
          </h2>

        </div>


        <div className="customer-quiz-step">

          {String(
            currentIndex +
              1,
          ).padStart(
            2,
            "0",
          )}


          <span>
            /
          </span>


          {String(
            questions.length,
          ).padStart(
            2,
            "0",
          )}

        </div>

      </header>


      <div className="customer-quiz-progress-track">

        <div
          className="customer-quiz-progress-value"
          style={{
            width:
              `${progress}%`,
          }}
        />

      </div>


      <div className="customer-quiz-question-area">

        <div className="customer-quiz-question-label">
          QUESTION{" "}
          {currentIndex + 1}
        </div>


        <h3>
          {currentQuestion.question}
        </h3>


        <p className="customer-quiz-hint">
          選擇最接近平常購物習慣的答案就可以囉！
        </p>

      </div>


      <div className="customer-quiz-options">

        {currentQuestion.options.map(
          (
            option,
            index,
          ) => {

            const selected =
              selectedOptionId ===
              option.id;


            return (
              <button
                key={
                  `${currentQuestion.id}-${option.id}-${index}`
                }
                type="button"
                className={`customer-quiz-option ${
                  selected
                    ? "selected"
                    : ""
                }`}
                onClick={() =>
                  handleSelectOption(
                    option.id,
                  )
                }
              >

                <span className="customer-quiz-option-marker">

                  {selected
                    ? "♥"
                    : String.fromCharCode(
                        65 +
                          index,
                      )}

                </span>


                <span className="customer-quiz-option-content">

                  <span className="customer-quiz-option-text">
                    {option.label}
                  </span>


                  {option.description ? (
                    <span className="customer-quiz-option-description">
                      {option.description}
                    </span>
                  ) : null}

                </span>


                <span
                  className={`customer-quiz-radio ${
                    selected
                      ? "selected"
                      : ""
                  }`}
                >

                  {selected && (
                    <span />
                  )}

                </span>

              </button>
            );
          },
        )}

      </div>


      <footer className="customer-quiz-footer">

        <button
          type="button"
          className="customer-quiz-back-button"
          onClick={
            handleBack
          }
          disabled={
            currentIndex ===
            0
          }
        >
          ← 上一題
        </button>


        <span className="customer-quiz-status">

          {selectedOptionId
            ? "已選擇答案 ♡"
            : "請選擇一個答案"}

        </span>


        <button
          type="button"
          className="customer-quiz-next-button"
          onClick={
            handleNext
          }
          disabled={
            !selectedOptionId
          }
        >

          {currentIndex ===
          questions.length -
            1
            ? "完成測驗 ♡"
            : "下一題 →"}

        </button>

      </footer>


      <style jsx>{`
        .customer-quiz-card {
          width: min(
            880px,
            100%
          );

          margin:
            18px auto
            32px;

          padding:
            30px 32px
            28px;

          position:
            relative;

          overflow:
            hidden;

          background:
            radial-gradient(
              circle at
              12% 10%,
              rgba(
                211,
                240,
                234,
                0.42
              ),
              transparent
              28%
            ),
            radial-gradient(
              circle at
              90% 90%,
              rgba(
                224,
                212,
                240,
                0.45
              ),
              transparent
              30%
            ),
            linear-gradient(
              145deg,
              rgba(
                255,
                255,
                255,
                0.97
              ),
              rgba(
                252,
                249,
                255,
                0.93
              )
            );

          border:
            3px solid
            #ffffff;

          outline:
            2px solid
            rgba(
              174,
              157,
              196,
              0.58
            );

          border-radius:
            30px;

          box-shadow:
            0 9px 0
            rgba(
              106,
              81,
              87,
              0.11
            ),
            0 18px 38px
            rgba(
              76,
              62,
              91,
              0.1
            );
        }


        .customer-quiz-card::before {
          content:
            "";

          position:
            absolute;

          inset:
            9px;

          pointer-events:
            none;

          border:
            1.5px dashed
            rgba(
              157,
              137,
              177,
              0.34
            );

          border-radius:
            21px;
        }


        .customer-quiz-header {
          position:
            relative;

          z-index:
            1;

          display:
            flex;

          align-items:
            flex-start;

          justify-content:
            space-between;

          gap:
            18px;

          margin-bottom:
            20px;
        }


        .customer-quiz-eyebrow {
          display:
            inline-flex;

          align-items:
            center;

          min-height:
            24px;

          margin-bottom:
            5px;

          padding:
            4px 10px;

          background:
            #e8f7f3;

          border:
            1px solid
            rgba(
              119,
              171,
              160,
              0.42
            );

          border-radius:
            999px;

          color:
            #628a81;

          font-size:
            10px;

          font-weight:
            900;

          letter-spacing:
            0.75px;
        }


        .customer-quiz-header h2 {
          margin:
            0;

          color:
            #5d444b;

          font-size:
            clamp(
              22px,
              3vw,
              28px
            );

          line-height:
            1.25;
        }


        .customer-quiz-step {
          flex-shrink:
            0;

          padding-top:
            7px;

          color:
            #765d7f;

          font-size:
            20px;

          font-weight:
            900;
        }


        .customer-quiz-step span {
          margin:
            0 4px;

          color:
            #bcaec4;

          font-size:
            13px;
        }


        .customer-quiz-progress-track {
          position:
            relative;

          z-index:
            1;

          width:
            100%;

          height:
            9px;

          overflow:
            hidden;

          margin-bottom:
            29px;

          background:
            rgba(
              230,
              220,
              236,
              0.62
            );

          border:
            1px solid
            rgba(
              150,
              130,
              163,
              0.18
            );

          border-radius:
            999px;
        }


        .customer-quiz-progress-value {
          height:
            100%;

          border-radius:
            inherit;

          background:
            linear-gradient(
              90deg,
              #b9dfd6
              0%,
              #b9dfd6
              52%,
              #c9b2dc
              100%
            );

          transition:
            width
            0.28s ease;
        }


        .customer-quiz-question-area {
          position:
            relative;

          z-index:
            1;

          text-align:
            center;

          padding:
            4px 18px
            23px;
        }


        .customer-quiz-question-label {
          display:
            inline-block;

          margin-bottom:
            11px;

          color:
            #997fa3;

          font-size:
            10px;

          font-weight:
            900;

          letter-spacing:
            1.3px;
        }


        .customer-quiz-question-area h3 {
          max-width:
            540px;

          margin:
            0 auto;

          color:
            #503b42;

          font-size:
            clamp(
              19px,
              2.5vw,
              23px
            );

          line-height:
            1.55;

          font-weight:
            900;
        }


        .customer-quiz-hint {
          margin:
            10px 0 0;

          color:
            #96868c;

          font-size:
            12px;
        }


        .customer-quiz-options {
          position:
            relative;

          z-index:
            1;

          display:
            grid;

          grid-template-columns:
            repeat(
              2,
              minmax(
                0,
                1fr
              )
            );

          gap:
            12px;
        }


        .customer-quiz-option {
          width:
            100%;

          min-height:
            86px;

          display:
            grid;

          grid-template-columns:
            38px 1fr
            24px;

          align-items:
            center;

          gap:
            11px;

          padding:
            14px 15px;

          border:
            1.5px solid
            rgba(
              135,
              114,
              145,
              0.23
            );

          border-radius:
            19px;

          background:
            linear-gradient(
              145deg,
              rgba(
                255,
                255,
                255,
                0.94
              ),
              rgba(
                250,
                247,
                252,
                0.88
              )
            );

          color:
            #604c52;

          text-align:
            left;

          cursor:
            pointer;
        }


        .customer-quiz-option:hover {
          transform:
            translateY(
              -2px
            );

          border-color:
            rgba(
              117,
              160,
              151,
              0.56
            );
        }


        .customer-quiz-option.selected {
          border-color:
            #8fbcb1;

          background:
            linear-gradient(
              145deg,
              #effaf7
              0%,
              #f7f0fb
              100%
            );
        }


        .customer-quiz-option-marker {
          width:
            34px;

          height:
            34px;

          display:
            flex;

          align-items:
            center;

          justify-content:
            center;

          border-radius:
            50%;

          background:
            #eee5f5;

          color:
            #725d7c;

          font-size:
            12px;

          font-weight:
            900;
        }


        .customer-quiz-option-content {
          display:
            flex;

          flex-direction:
            column;

          gap:
            3px;
        }


        .customer-quiz-option-text {
          font-size:
            13px;

          font-weight:
            800;
        }


        .customer-quiz-option-description {
          color:
            #94848a;

          font-size:
            11px;
        }


        .customer-quiz-radio {
          width:
            18px;

          height:
            18px;

          display:
            flex;

          align-items:
            center;

          justify-content:
            center;

          border:
            1.5px solid
            #c6b8ca;

          border-radius:
            50%;
        }


        .customer-quiz-radio.selected {
          border-color:
            #83aea4;
        }


        .customer-quiz-radio.selected span {
          width:
            8px;

          height:
            8px;

          border-radius:
            50%;

          background:
            #83aea4;
        }


        .customer-quiz-footer {
          position:
            relative;

          z-index:
            1;

          margin-top:
            27px;

          display:
            grid;

          grid-template-columns:
            130px 1fr
            150px;

          align-items:
            center;

          gap:
            14px;
        }


        .customer-quiz-back-button,
        .customer-quiz-next-button {
          min-height:
            44px;

          border-radius:
            999px;

          font-size:
            12px;

          font-weight:
            900;

          cursor:
            pointer;
        }


        .customer-quiz-back-button {
          border:
            1.5px solid
            rgba(
              124,
              102,
              132,
              0.24
            );

          background:
            rgba(
              255,
              255,
              255,
              0.72
            );

          color:
            #806d76;
        }


        .customer-quiz-next-button {
          border:
            0;

          background:
            linear-gradient(
              180deg,
              #80618e
              0%,
              #684d75
              100%
            );

          color:
            #ffffff;

          box-shadow:
            0 4px 0
            rgba(
              69,
              48,
              80,
              0.16
            );
        }


        .customer-quiz-back-button:disabled,
        .customer-quiz-next-button:disabled {
          cursor:
            not-allowed;

          opacity:
            0.4;
        }


        .customer-quiz-status {
          text-align:
            center;

          color:
            #9a8990;

          font-size:
            11px;

          font-weight:
            700;
        }


        @media (
          max-width:
          700px
        ) {
          .customer-quiz-card {
            padding:
              25px 20px
              22px;
          }


          .customer-quiz-options {
            grid-template-columns:
              1fr;
          }


          .customer-quiz-footer {
            grid-template-columns:
              1fr 1fr;
          }


          .customer-quiz-status {
            grid-column:
              1 / -1;

            grid-row:
              1;
          }
        }
      `}</style>

    </section>
  );
}
