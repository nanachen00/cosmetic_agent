import Link from "next/link";

export default function NotFound() {
  return <div className="content-wrap not-found"><div className="retro-frame"><span>404</span><h1>這一頁好像去補妝了</h1><p>請回到首頁重新選擇功能。</p><Link className="glossy-primary" href="/">回到首頁</Link></div></div>;
}
