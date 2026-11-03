export type ChatFlow =
  | "auto"
  | "product_search"
  | "skin_record"
  | "skin_consult"
  | "customer_quiz";

export interface ChatFlowConfig {
  assistantName: string;
  greeting: string;
  inputPlaceholder: string;
}

export const CHAT_FLOW_CONFIG: Record<
  ChatFlow,
  ChatFlowConfig
> = {
  auto: {
    assistantName: "BLINK AI",
    greeting:
      "嗨～你可以直接告訴我想找產品、比較商品、詢問成分，或分享使用感受。",
    inputPlaceholder:
      "直接輸入任何保養相關問題...",
  },
  product_search: {
    assistantName: "BLINK AI",

    greeting:
      "嗨～我會先幫你整理膚質、需求、預算與偏好，再為你尋找適合的產品。請先告訴我，你目前最想改善什麼肌膚問題，或正在找哪一類產品呢？",

    inputPlaceholder:
      "例如：想找適合油肌的清爽保濕面膜……",
  },

  skin_record: {
    assistantName: "BLINK AI",

    greeting:
      "嗨～一起看看今天的肌膚在說什麼吧 ♡ 最近有哪個變化讓你特別在意？你可以直接說完整一點，例如「這兩天下巴冒了幾顆痘，摸起來有點痛」。",

    inputPlaceholder:
      "例如：今天下巴長痘痘，而且有點泛紅……",
  },

  skin_consult: {
    assistantName: "BLINK AI",

    greeting:
      "嗨～想聊聊最近的膚況，或有保養品、成分上的疑問嗎？直接告訴我就好，我會陪你一起整理出適合的保養方向 ♡",

    inputPlaceholder:
      "例如：最近兩頰洗完臉會刺痛和脫皮……",
  },

  customer_quiz: {
    assistantName: "BLINK AI",

    /*
     * 預設使用中性文案。
     *
     * 第一次測驗 / 重新測驗的差異，
     * 建議由 chat/page.tsx 根據：
     *
     * mode=start
     * mode=retake
     *
     * 再覆蓋 greeting。
     */
    greeting:
      "嗨～接下來我們來做個簡單的消費人格測驗吧！我會一次問你一題，請依照平常購買保養品時最接近你的想法回答就可以了 ♡",

    inputPlaceholder:
      "輸入你的回答……",
  },
};

export function isChatFlow(
  value: string | null,
): value is ChatFlow {
  return (
    value === "auto" ||
    value === "product_search" ||
    value === "skin_record" ||
    value === "skin_consult" ||
    value === "customer_quiz"
  );
}