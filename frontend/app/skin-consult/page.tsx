"use client";

import { useEffect, useState } from "react";
import { DoctorSticker } from "@/components/Illustrations";
import { Icon } from "@/components/Icon";
import { PageHeader } from "@/components/PageHeader";

type ChatMessage = { role: "assistant" | "user"; text: string };

function createReply(text: string): string {
  if (/多久|天|週/.test(text)) return "了解持續時間。請再告訴我主要發生在哪個部位，以及程度大約是 1–5 分中的幾分？";
  if (/臉頰|下巴|額頭|鼻子|眼周/.test(text)) return "收到部位資訊。最近是否更換保養品、清潔產品，或有熬夜、經期等變化？";
  return "我先幫你整理：請描述主要症狀、發生部位、持續多久，以及目前大約有多嚴重。";
}

export default function SkinConsultPage() {
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([
    { role: "assistant", text: "嗨，我會用一問一答的方式協助整理你的肌膚問題。最近最困擾你的狀況是什麼呢？" }
  ]);

  useEffect(() => {
    const incoming = new URLSearchParams(window.location.search).get("message") ?? "";
    if (!incoming) return;
    setMessages((current) => [...current, { role: "user", text: incoming }, { role: "assistant", text: createReply(incoming) }]);
  }, []);

  function send() {
    const text = input.trim();
    if (!text) return;
    setMessages((current) => [...current, { role: "user", text }, { role: "assistant", text: createReply(text) }]);
    setInput("");
  }

  return (
    <div className="content-wrap consult-page">
      <PageHeader eyebrow="SKIN Q&A" title="膚況諮詢" description="以一次一題的方式補齊症狀、部位、程度與持續時間。"/>

      <section className="consult-window retro-frame">
        <div className="consult-header"><DoctorSticker/><div><strong>BLINK 膚況小幫手</strong><span><i/>線上</span></div><button>結束諮詢</button></div>
        <div className="chat-thread">
          {messages.map((message, index) => <div className={`chat-message ${message.role}`} key={`${message.role}-${index}`}>{message.role === "assistant" ? <span><img src="/blink-ai-avatar.svg" alt="" /></span> : <span>你</span>}<p>{message.text}</p></div>)}
        </div>
        <div className="consult-composer"><textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} placeholder="輸入你的回答……"/><button onClick={send}><Icon name="send"/></button></div>
        <div className="consult-tip">此區為前端互動示範；正式版本請串接 Root Agent／Skin Consult Flow。</div>
      </section>
    </div>
  );
}
