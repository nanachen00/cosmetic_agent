"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  useRouter,
} from "next/navigation";

import {
  PageHeader,
} from "@/components/PageHeader";

import {
  Icon,
} from "@/components/Icon";

import {
  AccountSecurityPanel,
} from "@/components/AccountSecurityPanel";

import {
  getProfile,
  saveProfile,
} from "@/lib/api";

import {
  getAccountSecurity,
} from "@/lib/account-api";

import {
  readSession,
  removeSession,
  writeSession,
} from "@/lib/storage";

import {
  getLatestQuizResult,
  type LatestQuizResult,
} from "@/lib/quiz-api";

import type {
  ProfileForm,
} from "@/types";


/* =========================================================
   Storage Keys
========================================================= */

const LOGIN_USER_ID_KEY =
  "blink_user_id";

const DISPLAY_NAME_KEY =
  "blink_display_name";

const USERNAME_KEY =
  "blink_username";

const AUTH_PROVIDER_KEY =
  "blink_auth_provider";

const EMAIL_KEY =
  "blink_email";


/* =========================================================
   Options
========================================================= */

const skinTypes = [
  "油性肌",
  "乾性肌",
  "中性肌",
  "混合肌",
];


const MAX_OTHER_ALLERGEN_LENGTH = 30;


const allergenOptionGroups = [
  {
    label: "香味與植物成分",
    options: [
      { value: "香料", label: "香精／香料（Fragrance／Parfum）" },
      { value: "精油", label: "精油／植物香料" },
      { value: "常見香料成分", label: "常見香料成分（Limonene／Linalool）" },
      { value: "茶樹精油", label: "茶樹精油" },
      { value: "蜂膠", label: "蜂膠（Propolis）" },
    ],
  },
  {
    label: "防腐劑",
    options: [
      { value: "MI／MCI 防腐劑", label: "異噻唑啉酮類（MI／MCI）" },
      { value: "甲醛釋放型防腐劑", label: "甲醛釋放型防腐劑" },
      { value: "苯氧乙醇", label: "苯氧乙醇（Phenoxyethanol）" },
      { value: "IPBC 防腐劑", label: "IPBC 防腐劑" },
      { value: "苯扎氯銨", label: "苯扎氯銨（Benzalkonium Chloride）" },
    ],
  },
  {
    label: "清潔與起泡成分",
    options: [
      { value: "椰油醯胺丙基甜菜鹼", label: "椰油醯胺丙基甜菜鹼（CAPB）" },
      { value: "葡糖苷類清潔成分", label: "葡糖苷類清潔成分" },
      { value: "椰油醯胺 DEA", label: "椰油醯胺 DEA（Cocamide DEA）" },
    ],
  },
  {
    label: "彩妝、染髮與美甲成分",
    options: [
      { value: "PPD 染髮劑", label: "染髮劑成分（PPD）" },
      { value: "壓克力酸酯", label: "美甲膠／睫毛膠成分（Acrylates／HEMA）" },
      { value: "胭脂紅", label: "胭脂紅（Carmine）" },
      { value: "蟲膠", label: "蟲膠（Shellac）" },
    ],
  },
  {
    label: "天然材質、油脂與金屬",
    options: [
      { value: "羊毛脂", label: "羊毛脂（Lanolin）" },
      { value: "乳膠", label: "天然乳膠" },
      { value: "鎳", label: "鎳" },
      { value: "鈷", label: "鈷" },
    ],
  },
  {
    label: "防曬成分",
    options: [
      { value: "二苯甲酮-3", label: "二苯甲酮-3（Oxybenzone）" },
      { value: "奧克立林", label: "奧克立林（Octocrylene）" },
      { value: "桂皮酸鹽防曬劑", label: "桂皮酸鹽防曬劑（Octinoxate）" },
    ],
  },
  {
    label: "容易刺激或不耐受的成分",
    options: [
      { value: "酒精", label: "酒精" },
      { value: "水楊酸", label: "水楊酸（BHA）" },
      { value: "果酸", label: "果酸（AHA）" },
      { value: "維生素 A 類", label: "維生素 A 類（A 醇／Retinoids）" },
      { value: "過氧化苯", label: "過氧化苯（Benzoyl Peroxide）" },
      { value: "高濃度維生素 C", label: "高濃度維生素 C" },
    ],
  },
];


type ProfileFieldIconName =
  | "name"
  | "email"
  | "birthday"
  | "gender";


function ProfileFieldIcon({
  name,
}: {
  name: ProfileFieldIconName;
}) {
  const iconSources:
  Record<ProfileFieldIconName, string> = {
    name: "/profile-field-icons/name-v3.png",
    email: "/profile-field-icons/email-v3.png",
    birthday: "/profile-field-icons/birthday-v3.png",
    gender: "/profile-field-icons/gender-v3.png",
  };

  return (
    <img
      src={iconSources[name]}
      alt=""
      aria-hidden="true"
      className="profile-field-icon"
    />
  );
}


/* =========================================================
   Customer Type Display
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


const customerTypeDescriptions:
Record<string, string> = {
  hesitant_decision_maker:
    "你通常會先蒐集資訊、比較差異，再做出購買決定。",

  ingredient_researcher:
    "你在選購保養品時，會特別關注成分與配方資訊。",

  price_sensitive:
    "你在選購商品時，會特別留意價格、折扣與整體 CP 值。",

  review_dependent:
    "你在購買前，通常會參考其他消費者的評價與使用心得。",

  brand_oriented:
    "你在選購商品時，較重視熟悉或信任的品牌。",

  explorer:
    "你喜歡探索新品，也願意嘗試不同品牌與產品。",

  function_oriented:
    "你挑選商品時，會優先考量實際功效是否符合需求。",

  impulse_buyer:
    "你在遇到喜歡或吸引你的商品時，通常會較快做出購買決定。",
};


/* =========================================================
   Empty Profile
========================================================= */

const initialProfile:
ProfileForm = {
  name:
    "",

  gender:
    "不透露",

  birthday:
    "",

  email:
    "",

  lineBound:
    false,

  skinType:
    "",

  allergens:
    [],

  otherAllergen:
    "",

  customerType:
    "",

  wellnessStatus:
    "",

  periodStartDate:
    "",

  periodEndDate:
    "",
};


/* =========================================================
   Allergen Helpers
========================================================= */

function parseAllergenInput(
  value: string,
): string[] {
  return (
    value
      ?? ""
  )
    .split(
      /[、,，]/,
    )
    .map(
      (
        item,
      ) =>
        item.trim(),
    )
    .filter(
      Boolean,
    );
}


function getLocalDateInputValue(
  date: Date,
): string {
  const year =
    date.getFullYear();

  const month =
    String(
      date.getMonth() + 1,
    ).padStart(
      2,
      "0",
    );

  const day =
    String(
      date.getDate(),
    ).padStart(
      2,
      "0",
    );

  return (
    `${year}-${month}-${day}`
  );
}


function commitAllergenDraft(
  profile: ProfileForm,
): ProfileForm {
  const newItems =
    parseAllergenInput(
      profile.otherAllergen,
    );


  if (
    newItems.length ===
    0
  ) {
    return {
      ...profile,

      email:
        profile.email ?? "",

      otherAllergen:
        "",
    };
  }


  const mergedAllergens =
    Array.from(
      new Set([
        ...(profile.allergens ?? []),
        ...newItems,
      ]),
    );


  return {
    ...profile,

    email:
      profile.email ?? "",

    allergens:
      mergedAllergens,

    otherAllergen:
      "",
  };
}


function isValidEmail(
  value: string,
): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/
    .test(
      (
        value
        ?? ""
      ).trim(),
    );
}


/* =========================================================
   Page
========================================================= */

export default function ProfilePage() {
  const router =
    useRouter();


  const todayDate =
    getLocalDateInputValue(
      new Date(),
    );


  const [
    profile,
    setProfile,
  ] =
    useState<ProfileForm>(
      initialProfile,
    );


  const [
    savedProfile,
    setSavedProfile,
  ] =
    useState<ProfileForm>(
      initialProfile,
    );


  const [
    saving,
    setSaving,
  ] =
    useState(
      false,
    );


  const [
    notice,
    setNotice,
  ] =
    useState(
      "",
    );


  /* =======================================================
     Current User
  ======================================================= */

  const [
    currentUserId,
    setCurrentUserId,
  ] =
    useState(
      "",
    );


  const [
    authState,
    setAuthState,
  ] =
    useState<
      "loading" |
      "authenticated" |
      "error"
    >(
      "loading",
    );


  const [
    username,
    setUsername,
  ] =
    useState(
      "",
    );


  const [
    verifiedEmail,
    setVerifiedEmail,
  ] =
    useState(
      "",
    );


  const [
    emailVerified,
    setEmailVerified,
  ] =
    useState(
      false,
    );


  const [
    emailTouched,
    setEmailTouched,
  ] =
    useState(
      false,
    );


  const [
    hasEditedThisVisit,
    setHasEditedThisVisit,
  ] =
    useState(
      false,
    );


  const [
    pendingNavigation,
    setPendingNavigation,
  ] =
    useState(
      "",
    );


  const allowLeaveRef =
    useRef(
      false,
    );


  /* =======================================================
     Quiz State
  ======================================================= */

  const [
    latestQuiz,
    setLatestQuiz,
  ] =
    useState<
      LatestQuizResult |
      null
    >(
      null,
    );


  const [
    quizLoading,
    setQuizLoading,
  ] =
    useState(
      true,
    );


  const [
    quizLoadError,
    setQuizLoadError,
  ] =
    useState(
      "",
    );


  /* =======================================================
     Load Profile + Login Identity + Latest Quiz
  ======================================================= */

  useEffect(() => {
    let cancelled =
      false;


    async function loadPage() {
      /* ===================================================
         1. 取得目前真正登入中的帳號
      =================================================== */

      let userId =
        "";


      let account:
        Awaited<
          ReturnType<
            typeof getAccountSecurity
          >
        > |
        null =
          null;


      try {
        account =
          await getAccountSecurity();


        userId =
          (
            account.user_id
            ?? ""
          ).trim();


        if (
          !userId
        ) {
          throw new Error(
            "登入服務未回傳 user_id",
          );
        }


        /*
         * SessionStorage 永遠只寫入字串。
         *
         * LINE 舊帳號可能：
         * email = null
         *
         * 不可以把 null 原封不動存進前端身份資料。
         */

        writeSession(
          LOGIN_USER_ID_KEY,
          userId,
        );


        writeSession(
          USERNAME_KEY,
          account.username ?? "",
        );


        writeSession(
          EMAIL_KEY,
          account.email ?? "",
        );


        if (
          account.display_name?.trim()
        ) {
          writeSession(
            DISPLAY_NAME_KEY,
            account.display_name.trim(),
          );
        }

      } catch (
        error
      ) {
        console.warn(
          "Load account session failed:",
          error,
        );


        if (
          !cancelled
        ) {
          setAuthState(
            "error",
          );

          router.replace(
            "/login?next=%2Fprofile",
          );
        }


        return;
      }


      if (
        !cancelled
      ) {
        setCurrentUserId(
          userId,
        );

        setAuthState(
          "authenticated",
        );
      }


      const profileRequest = getProfile(userId);
      const latestQuizRequest = getLatestQuizResult(userId);

      /* ===================================================
         2. 登入方式 + 帳號 Email
      =================================================== */

      const provider =
        readSession<string>(
          AUTH_PROVIDER_KEY,
          "",
        );


      const storedUsername =
        readSession<string>(
          USERNAME_KEY,
          "",
        );


      const loginUsername =
        String(
          account?.username
          ?? storedUsername
          ?? ""
        ).trim();


      if (
        !cancelled
      ) {
        setUsername(
          loginUsername,
        );
      }


      const storedEmail =
        readSession<string>(
          EMAIL_KEY,
          "",
        );


      /*
       * 最重要的修正：
       *
       * account.email 可以是 null。
       *
       * 一律先轉成安全字串，
       * 後面才可以使用 trim()。
       */

      const loginEmail =
        String(
          account?.email
          ?? storedEmail
          ?? ""
        ).trim();


      if (
        !cancelled
      ) {
        setVerifiedEmail(
          loginEmail,
        );


        setEmailVerified(
          Boolean(
            account?.email_verified
            && loginEmail
          ),
        );
      }


      /* ===================================================
         3. 每個 user 使用自己的 Profile key
      =================================================== */

      const profileStorageKey =
        `blink-profile:${userId}`;


      const storedProfile =
        readSession<ProfileForm>(
          profileStorageKey,
          initialProfile,
        );


      let serverProfile:
        Partial<ProfileForm> = {};


      try {
        serverProfile =
          await profileRequest;

      } catch (
        error
      ) {
        console.warn(
          "Load server profile failed:",
          error,
        );
      }


      /* ===================================================
         4. 登入名稱
      =================================================== */

      const storedDisplayName =
        readSession<string>(
          DISPLAY_NAME_KEY,
          "",
        );


      const accountDisplayName =
        (
          account?.display_name
          ?? ""
        ).trim();


      const displayName =
        accountDisplayName
        || (
          storedDisplayName
          ?? ""
        ).trim();


      /* ===================================================
         5. LINE 綁定狀態
      =================================================== */

      const lineBound =
        account
          ? (
              account.providers
              ?? []
            ).includes(
              "line",
            )
          : provider ===
            "line";


      /*
       * 舊資料可能曾經存過：
       *
       * email: null
       *
       * 因此這裡最後一定重新正規化。
       */

      const resolvedProfile:
      ProfileForm = {
        ...initialProfile,

        ...storedProfile,

        ...serverProfile,

        name:
          String(
            serverProfile.name
            ?? storedProfile.name
            ?? ""
          ),

        email:
          String(
            serverProfile.email
            ?? storedProfile.email
            ?? ""
          ),

        allergens:
          serverProfile.allergens
          ?? storedProfile.allergens
          ?? [],
      };


      const mergedProfile:
      ProfileForm = {
        ...resolvedProfile,


        name:
          resolvedProfile
            .name
            ?.trim()
            ? resolvedProfile.name
            : displayName,


        /*
         * 登入系統的 Email 優先。
         *
         * LINE OAuth 成功取得 Email 後，
         * account.email 就會進這裡。
         */

        email:
          account?.email?.trim()
          || resolvedProfile.email?.trim()
          || loginEmail
          || "",


        lineBound,
      };


      /* ===================================================
         Profile Draft
      =================================================== */

      const profileDraftKey =
        `blink-profile-draft:${userId}`;


      const draftProfile =
        readSession<
          ProfileForm |
          null
        >(
          profileDraftKey,
          null,
        );


      const safeDraftProfile:
        ProfileForm |
        null =
        draftProfile
          ? {
              ...initialProfile,

              ...draftProfile,

              name:
                String(
                  draftProfile.name
                  ?? ""
                ),

              email:
                String(
                  draftProfile.email
                  ?? mergedProfile.email
                  ?? ""
                ),

              allergens:
                draftProfile.allergens
                ?? [],
            }
          : null;


      if (
        !cancelled
      ) {
        setProfile(
          safeDraftProfile
          ?? mergedProfile,
        );


        setSavedProfile(
          mergedProfile,
        );


        if (
          safeDraftProfile
        ) {
          setNotice(
            "已恢復上次未儲存的草稿。",
          );
        }
      }


      /* ===================================================
         6. 最新 Quiz
      =================================================== */

      try {
        setQuizLoading(
          true,
        );


        setQuizLoadError(
          "",
        );


        const response =
          await latestQuizRequest;


        if (
          cancelled
        ) {
          return;
        }


        if (
          response.has_completed &&
          response.result
        ) {
          setLatestQuiz(
            response.result,
          );

        } else {
          setLatestQuiz(
            null,
          );
        }

      } catch (
        error
      ) {
        console.error(
          "Load latest quiz failed:",
          error,
        );


        if (
          !cancelled
        ) {
          setLatestQuiz(
            null,
          );


          setQuizLoadError(
            error instanceof
              Error
              ? error.message
              : "無法取得消費人格結果",
          );
        }

      } finally {
        if (
          !cancelled
        ) {
          setQuizLoading(
            false,
          );
        }
      }
    }


    void loadPage();


    return () => {
      cancelled =
        true;
    };

  }, [
    router,
  ]);


  /* =======================================================
     Email
  ======================================================= */

  const normalizedEmail =
    (
      profile.email
      ?? ""
    ).trim();


  const emailIsValid =
    !normalizedEmail
    ||
    isValidEmail(
      normalizedEmail,
    );


  const emailError =
    emailTouched
      ? (
          normalizedEmail &&
          !emailIsValid
            ? "Email 格式不正確"
            : ""
        )
      : "";


  const emailIsVerified =
    emailVerified
    &&
    emailIsValid
    &&
    normalizedEmail
      .toLowerCase()
    ===
    (
      verifiedEmail
      ?? ""
    )
      .trim()
      .toLowerCase();


  /* =======================================================
     Dirty State
  ======================================================= */

  const isDirty =
    useMemo(
      () =>
        JSON.stringify(
          profile,
        )
        !==
        JSON.stringify(
          savedProfile,
        ),
      [
        profile,
        savedProfile,
      ],
    );


  useEffect(() => {
    if (
      !currentUserId
    ) {
      return;
    }


    const profileDraftKey =
      `blink-profile-draft:${currentUserId}`;


    if (
      isDirty
    ) {
      writeSession(
        profileDraftKey,
        profile,
      );

    } else {
      removeSession(
        profileDraftKey,
      );
    }

  }, [
    currentUserId,
    isDirty,
    profile,
  ]);


  const shouldBlockNavigation =
    isDirty
    &&
    hasEditedThisVisit;


  useEffect(() => {
    if (
      !shouldBlockNavigation
    ) {
      return;
    }


    const handleBeforeUnload =
      (
        event:
          BeforeUnloadEvent,
      ) => {
        if (
          allowLeaveRef.current
        ) {
          return;
        }


        event.preventDefault();

        event.returnValue =
          "";
      };


    const handleDocumentClick =
      (
        event:
          MouseEvent,
      ) => {
        if (
          !(
            event.target
            instanceof Element
          )
        ) {
          return;
        }


        const link =
          event.target.closest<
            HTMLAnchorElement
          >(
            "a[href]",
          );


        if (
          !link
          ||
          link.target
          ||
          link.hasAttribute(
            "download",
          )
        ) {
          return;
        }


        const destination =
          new URL(
            link.href,
            window.location.href,
          );


        if (
          destination.href
          ===
          window.location.href
        ) {
          return;
        }


        event.preventDefault();

        event.stopPropagation();


        setPendingNavigation(
          destination.href,
        );
      };


    window.addEventListener(
      "beforeunload",
      handleBeforeUnload,
    );


    document.addEventListener(
      "click",
      handleDocumentClick,
      true,
    );


    return () => {
      window.removeEventListener(
        "beforeunload",
        handleBeforeUnload,
      );


      document.removeEventListener(
        "click",
        handleDocumentClick,
        true,
      );
    };

  }, [
    shouldBlockNavigation,
  ]);


  function confirmNavigation() {
    if (
      !pendingNavigation
    ) {
      return;
    }


    const destination =
      new URL(
        pendingNavigation,
        window.location.href,
      );


    allowLeaveRef.current =
      true;


    setPendingNavigation(
      "",
    );


    if (
      destination.origin
      ===
      window.location.origin
    ) {
      router.push(
        destination.pathname
        +
        destination.search
        +
        destination.hash,
      );


      return;
    }


    window.location.assign(
      destination.href,
    );
  }


  /* =======================================================
     Profile Update
  ======================================================= */

  function update<
    K extends keyof ProfileForm
  >(
    key: K,
    value: ProfileForm[K],
  ) {
    setProfile(
      (
        current,
      ) => ({
        ...current,

        [key]:
          value,
      }),
    );


    setHasEditedThisVisit(
      true,
    );


    setNotice(
      "",
    );
  }


  /* =======================================================
     Allergen
  ======================================================= */

  function removeCustomAllergen(
    value: string,
  ) {
    setProfile(
      (
        current,
      ) => ({
        ...current,

        allergens:
          (
            current.allergens
            ?? []
          ).filter(
            (
              item,
            ) =>
              item
              !==
              value,
          ),

        otherAllergen:
          parseAllergenInput(
            current.otherAllergen
            ?? "",
          )
            .filter(
              (
                item,
              ) =>
                item
                !==
                value,
            )
            .join(
              "、",
            ),
      }),
    );


    setHasEditedThisVisit(
      true,
    );


    setNotice(
      "",
    );
  }


  function addOtherAllergen() {
    setHasEditedThisVisit(
      true,
    );


    setProfile(
      (
        current,
      ) =>
        commitAllergenDraft(
          current,
        ),
    );


    setNotice(
      "",
    );
  }


  function handleAllergenKeyDown(
    event:
      React.KeyboardEvent<
        HTMLInputElement
      >,
  ) {
    if (
      event.key
      !==
      "Enter"
    ) {
      return;
    }


    event.preventDefault();


    addOtherAllergen();
  }


  /* =======================================================
     Save
  ======================================================= */

  async function handleSave() {
    setEmailTouched(
      true,
    );


    if (
      authState
        !==
        "authenticated"
      ||
      !currentUserId
      ||
      saving
      ||
      !emailIsValid
    ) {
      return;
    }


    const profileToSave =
      commitAllergenDraft({
        ...profile,

        email:
          profile.email
          ?? "",
      });


    setProfile(
      profileToSave,
    );


    setSaving(
      true,
    );


    setNotice(
      "",
    );


    try {
      await saveProfile(
        profileToSave,
        currentUserId,
      );


      const profileStorageKey =
        `blink-profile:${currentUserId}`;


      writeSession(
        profileStorageKey,
        profileToSave,
      );


      if (
        (
          profileToSave.name
          ?? ""
        ).trim()
      ) {
        writeSession(
          DISPLAY_NAME_KEY,
          (
            profileToSave.name
            ?? ""
          ).trim(),
        );


        window.dispatchEvent(
          new Event(
            "blink-auth-updated",
          ),
        );
      }


      setSavedProfile(
        profileToSave,
      );


      setHasEditedThisVisit(
        false,
      );


      removeSession(
        `blink-profile-draft:${currentUserId}`,
      );


      setNotice(
        "資料已儲存完成 ♡",
      );

    } catch (
      error
    ) {
      console.error(
        "Profile save failed:",
        error,
      );


      setNotice(
        error instanceof
          Error
          ? error.message
          : "儲存失敗，請稍後再試。",
      );

    } finally {
      setSaving(
        false,
      );
    }
  }


  /* =======================================================
     Cancel
  ======================================================= */

  function handleCancel() {
    setProfile(
      savedProfile,
    );


    removeSession(
      `blink-profile-draft:${currentUserId}`,
    );


    setEmailTouched(
      false,
    );


    setHasEditedThisVisit(
      false,
    );


    setNotice(
      "",
    );
  }


  /* =======================================================
     Quiz Display
  ======================================================= */

  const hasCompletedQuiz =
    Boolean(
      latestQuiz
        ?.primary_type,
    );


  const primaryType =
    latestQuiz
      ?.primary_type
    ?? "";


  const customerTypeLabel =
    customerTypeLabels[
      primaryType
    ]
    ??
    primaryType;


  const customerTypeDescription =
    customerTypeDescriptions[
      primaryType
    ]
    ??
    (
      hasCompletedQuiz
        ? "BLINK AI 會依照你的消費偏好，提供更符合你的商品資訊與推薦方式。"
        : "完成小測驗，讓 BLINK AI 更了解你的購物習慣與偏好。"
    );


  /* =======================================================
     Start / Retake Quiz
  ======================================================= */

  function handleQuiz() {
    const mode =
      hasCompletedQuiz
        ? "retake"
        : "start";


    if (
      currentUserId
    ) {
      writeSession(
        `blink-profile-draft:${currentUserId}`,
        {
          ...profile,

          email:
            profile.email
            ?? "",
        },
      );
    }


    allowLeaveRef.current =
      true;


    router.push(
      `/personality-quiz?mode=${mode}`,
    );
  }


  /* =======================================================
     Render
  ======================================================= */

  if (
    authState
    !==
    "authenticated"
  ) {
    return (
      <div
        className="content-wrap profile-page"
        role="status"
        aria-live="polite"
      >
        {authState ===
        "loading"
          ? "正在驗證登入狀態…"
          : "登入狀態無效，正在返回登入頁…"}
      </div>
    );
  }


  return (
    <div className="content-wrap profile-page">

      <PageHeader
        eyebrow="MY PROFILE"
        title="基本資料"
        description=""
      />


      {/* =========================
          基本資訊
      ========================== */}

      <section className="retro-frame profile-section">

        <span className="section-tab">
          基本資訊
        </span>


        <div className="form-grid profile-basic-grid">

          <label>

            <span className="profile-field-label">
              姓名
            </span>


            <input
              value={
                profile.name
                ?? ""
              }
              onChange={(
                event,
              ) =>
                update(
                  "name",
                  event.target.value,
                )
              }
              placeholder="請輸入姓名"
            />

          </label>


          <label>

            <span className="profile-field-label">
              Email
            </span>


            <div className="profile-email-control">

              <input
                type="email"
                value={
                  profile.email
                  ?? ""
                }
                readOnly
                aria-readonly="true"
                title="如需更換 Email，請從登入與安全設定"
                onBlur={() =>
                  setEmailTouched(
                    true,
                  )
                }
                aria-invalid={
                  Boolean(
                    emailError,
                  )
                }
                aria-describedby={
                  emailError
                  ||
                  (
                    !emailIsVerified
                    &&
                    emailIsValid
                  )
                    ? "profile-email-feedback"
                    : undefined
                }
                placeholder="name@example.com"
                style={
                  emailError
                    ? {
                        borderColor:
                          "#c94f68",

                        boxShadow:
                          "0 0 0 3px rgba(201, 79, 104, 0.16)",
                      }
                    : undefined
                }
              />


              {emailIsVerified &&
              !emailError ? (

                <span
                  className="profile-email-verified"
                  aria-label="Email 已驗證"
                  title="Email 已驗證"
                >
                  <Icon name="check" />
                </span>

              ) : null}

            </div>


            {emailError
            ||
            (
              !emailIsVerified
              &&
              normalizedEmail
              &&
              emailIsValid
            ) ? (

              <small
                id="profile-email-feedback"
                className={
                  emailError
                    ? "profile-email-feedback error"
                    : "profile-email-feedback"
                }
              >
                {emailError ||
                  "尚未驗證"}
              </small>

            ) : null}


            {!normalizedEmail
            &&
            profile.lineBound ? (

              <small
                id="profile-email-feedback"
                className="profile-email-feedback"
              >
                LINE 未提供 Email，可先儲存其他基本資料
              </small>

            ) : null}

          </label>


          <label>

            <span className="profile-field-label">
              生日
            </span>


            <input
              type="date"
              value={
                profile.birthday
                ?? ""
              }
              onChange={(
                event,
              ) =>
                update(
                  "birthday",
                  event.target.value,
                )
              }
            />

          </label>


          <label>

            <span className="profile-field-label">
              性別
            </span>


            <select
              value={
                profile.gender
                ?? "不透露"
              }
              onChange={(
                event,
              ) =>
                update(
                  "gender",
                  event.target.value,
                )
              }
            >

              <option>
                女
              </option>

              <option>
                男
              </option>

              <option>
                其他
              </option>

              <option>
                不透露
              </option>

            </select>

          </label>


          <AccountSecurityPanel />

        </div>

      </section>


      {/* =========================
          肌膚資訊
      ========================== */}

      <section className="retro-frame profile-section skin-profile-section">

        <span className="section-tab lavender">
          肌膚資訊
        </span>


        <div className="choice-row">

          <strong>
            目前膚質
          </strong>


          <div className="choice-list">

            {skinTypes.map(
              (
                item,
              ) => (

                <button
                  key={
                    item
                  }
                  type="button"
                  className={
                    profile.skinType
                    ===
                    item
                      ? "selected"
                      : ""
                  }
                  onClick={() =>
                    update(
                      "skinType",
                      item,
                    )
                  }
                >
                  {item}
                </button>

              ),
            )}

          </div>

        </div>


        <div className="choice-row align-start allergen-row">

          <strong>
            禁忌成分
          </strong>


          <div className="allergen-area">

            {(profile.allergens ?? []).length > 0 ? (

              <div className="choice-list selected-allergen-list">

                {(profile.allergens ?? []).map(
                  (
                    item,
                  ) => (

                    <button
                      key={
                        item
                      }
                      type="button"
                      className="allergen-chip"
                      title={`移除 ${item}`}
                      onClick={() =>
                        removeCustomAllergen(
                          item,
                        )
                      }
                    >
                      <span>
                        {item}
                      </span>

                      <span aria-hidden="true">
                        ×
                      </span>
                    </button>

                  ),
                )}

              </div>

            ) : null}


            <select
              className="allergen-preset-select"
              value=""
              aria-label="選擇常見禁忌成分"
              onChange={(
                event,
              ) => {
                if (
                  event.target.value
                ) {
                  update(
                    "otherAllergen",
                    event.target.value,
                  );
                }
              }}
            >

              <option value="">
                選擇常見禁忌成分
              </option>


              {allergenOptionGroups.map(
                (
                  group,
                ) => (

                  <optgroup
                    key={
                      group.label
                    }
                    label={
                      group.label
                    }
                  >

                    {group.options.map(
                      (
                        option,
                      ) => (

                        <option
                          key={
                            option.value
                          }
                          value={
                            option.value
                          }
                          disabled={
                            (
                              profile.allergens
                              ?? []
                            ).includes(
                              option.value,
                            )
                          }
                        >
                          {option.label}
                        </option>

                      ),
                    )}

                  </optgroup>

                ),
              )}

            </select>


            <div className="allergen-input-wrap allergen-composer">

              <input
                value={
                  profile.otherAllergen
                  ?? ""
                }
                maxLength={
                  MAX_OTHER_ALLERGEN_LENGTH
                }
                onChange={(
                  event,
                ) =>
                  update(
                    "otherAllergen",
                    event.target.value,
                  )
                }
                onKeyDown={
                  handleAllergenKeyDown
                }
                placeholder="其他成分名稱"
                aria-label="輸入其他禁忌成分"
              />


              <button
                type="button"
                className="allergen-add-button"
                disabled={
                  !(
                    profile.otherAllergen
                    ?? ""
                  ).trim()
                }
                onClick={
                  addOtherAllergen
                }
              >
                ＋ 加入
              </button>

            </div>

          </div>

        </div>


        <div className="profile-soft-divider" />


        <div className="wellness-heading">

          <strong>
            週期與身體狀態
          </strong>

          <span>
            選填
          </span>

        </div>


        <div className="wellness-grid">

          <label>

            <span>
              目前狀態
            </span>

            <select
              value={
                profile.wellnessStatus
                ?? ""
              }
              onChange={(
                event,
              ) =>
                update(
                  "wellnessStatus",
                  event.target.value,
                )
              }
            >

              <option value="">
                未設定
              </option>

              <option value="none">
                目前無特殊狀態
              </option>

              <option value="trying">
                備孕中
              </option>

              <option value="pregnant">
                懷孕中
              </option>

              <option value="breastfeeding">
                哺乳中
              </option>

              <option value="prefer_not_to_say">
                不方便透露
              </option>

            </select>

          </label>


          <label>

            <span>
              最近一次月經開始日
            </span>

            <input
              type="date"
              max={
                todayDate
              }
              value={
                profile.periodStartDate
                ?? ""
              }
              onChange={(
                event,
              ) => {
                const value =
                  event.target.value;


                if (
                  value
                  &&
                  value > todayDate
                ) {
                  return;
                }


                setProfile(
                  (
                    current,
                  ) => ({
                    ...current,

                    periodStartDate:
                      value,

                    periodEndDate:
                      current.periodEndDate
                      &&
                      value
                      &&
                      current.periodEndDate
                      <
                      value
                        ? ""
                        : current.periodEndDate,
                  }),
                );


                setHasEditedThisVisit(
                  true,
                );


                setNotice(
                  "",
                );
              }}
            />

          </label>


          <label>

            <span>
              最近一次月經結束日
            </span>

            <input
              type="date"
              min={
                profile.periodStartDate
                || undefined
              }
              max={
                todayDate
              }
              value={
                profile.periodEndDate
                ?? ""
              }
              onChange={(
                event,
              ) => {
                const value =
                  event.target.value;


                if (
                  (
                    value
                    &&
                    value > todayDate
                  )
                  ||
                  (
                    value
                    &&
                    profile.periodStartDate
                    &&
                    value
                    <
                    profile.periodStartDate
                  )
                ) {
                  return;
                }


                update(
                  "periodEndDate",
                  value,
                );
              }}
            />

          </label>

        </div>

      </section>


      {/* =========================
          消費人格
      ========================== */}

      <section className="retro-frame personality-card">

        <span
          className="personality-emblem"
          aria-hidden="true"
        >
          <span>
            {quizLoading
              ? "…"
              : hasCompletedQuiz
                ? "★"
                : "?"}
          </span>
        </span>


        <div className="personality-copy">

          <small>
            你的消費人格
          </small>


          <h2>
            {quizLoading
              ? "讀取中…"
              : hasCompletedQuiz
                ? customerTypeLabel
                : "尚未測驗"}
          </h2>


          <p>
            {quizLoading
              ? "正在讀取消費人格資料…"
              : customerTypeDescription}
          </p>


          {quizLoadError ? (

            <small>
              暫時無法取得最新測驗結果
            </small>

          ) : null}

        </div>


        <button
          type="button"
          className="outline-button personality-retest-button"
          onClick={
            handleQuiz
          }
          disabled={
            quizLoading
          }
        >

          <span
            className="personality-refresh-icon"
            aria-hidden="true"
          />


          {hasCompletedQuiz
            ? "重新測驗"
            : "開始測驗"}

        </button>

      </section>


      {/* =========================
          底部操作
      ========================== */}

      <div className="page-actions profile-page-actions">

        {notice ? (

          <span className="save-notice">
            {notice}
          </span>

        ) : null}


        <button
          type="button"
          className="ghost-button"
          onClick={
            handleCancel
          }
          disabled={
            saving
          }
        >
          取消
        </button>


        <button
          type="button"
          className="glossy-primary"
          onClick={
            handleSave
          }
          disabled={
            authState
              !==
              "authenticated"
            ||
            saving
            ||
            !emailIsValid
          }
        >
          {saving
            ? "儲存中…"
            : "儲存變更"}
        </button>

      </div>


      {pendingNavigation ? (

        <div
          className="profile-leave-backdrop"
          role="presentation"
        >

          <section
            className="profile-leave-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="profile-leave-title"
            aria-describedby="profile-leave-description"
          >

            <span className="profile-leave-kicker">
              UNSAVED CHANGES
            </span>


            <h2 id="profile-leave-title">
              資料還沒儲存喔
            </h2>


            <p id="profile-leave-description">
              這次的修改會保留成草稿。你要繼續編輯，還是先離開這一頁？
            </p>


            <div className="profile-leave-actions">

              <button
                type="button"
                className="ghost-button"
                onClick={() =>
                  setPendingNavigation(
                    "",
                  )
                }
              >
                繼續編輯
              </button>


              <button
                type="button"
                className="glossy-primary"
                onClick={
                  confirmNavigation
                }
              >
                保留草稿並離開
              </button>

            </div>

          </section>


          <style jsx>{`
            .profile-leave-backdrop {
              position: fixed;
              inset: 0;
              z-index: 2000;
              display: grid;
              place-items: center;
              padding: 24px;
              background: rgba(91, 72, 94, 0.34);
              backdrop-filter: blur(7px);
            }

            .profile-leave-dialog {
              width: min(460px, 100%);
              padding: 34px 30px 28px;
              text-align: center;
              color: #6f5359;
              background:
                radial-gradient(
                  circle at top right,
                  rgba(226, 244, 237, 0.8),
                  transparent 42%
                ),
                #fffdf9;
              border: 4px solid #ffffff;
              outline: 3px solid #c7a7d5;
              border-radius: 30px;
              box-shadow:
                0 18px 45px rgba(102, 76, 108, 0.24),
                0 7px 0 rgba(183, 151, 197, 0.24);
            }

            .profile-leave-kicker {
              display: inline-flex;
              padding: 7px 14px;
              margin-bottom: 14px;
              color: #ffffff;
              font-size: 12px;
              font-weight: 900;
              letter-spacing: 0.13em;
              background: #a889b9;
              border-radius: 999px;
              box-shadow: 0 3px 0 #80658f;
            }

            .profile-leave-dialog h2 {
              margin: 0 0 10px;
              font-size: clamp(24px, 5vw, 32px);
              color: #694f56;
            }

            .profile-leave-dialog p {
              margin: 0 auto 24px;
              max-width: 340px;
              line-height: 1.75;
              color: #90777d;
            }

            .profile-leave-actions {
              display: flex;
              justify-content: center;
              gap: 12px;
              flex-wrap: wrap;
            }
          `}</style>

        </div>

      ) : null}

    </div>
  );
}
