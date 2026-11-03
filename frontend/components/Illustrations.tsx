export function BlinkGirlSticker() {
  return (
    <div className="girl-sticker" aria-hidden="true">
      <span className="sticker-caption">BLINK GIRL</span>
      <svg viewBox="0 0 260 260">
        <path className="sticker-cloud" d="M45 80c-18-35 18-61 48-43 13-31 62-29 72 4 30-15 64 9 50 43 32 9 35 53 6 68 18 29-12 61-43 48-14 32-59 32-73 3-29 17-65-8-53-40-35-8-40-55-7-83Z"/>
        <circle cx="132" cy="91" r="49" className="girl-face"/>
        <path d="M86 91c-2-45 22-68 49-68 31 0 54 26 50 67-15-21-33-31-51-31-18 0-34 10-48 32Z" className="girl-hair"/>
        <path d="M92 81c-9 15-9 37 0 52M174 81c10 18 8 39 0 53" className="girl-hair-line"/>
        <ellipse cx="113" cy="93" rx="9" ry="14" className="girl-eye"/>
        <ellipse cx="151" cy="93" rx="9" ry="14" className="girl-eye"/>
        <circle cx="116" cy="88" r="3" className="girl-shine"/>
        <circle cx="154" cy="88" r="3" className="girl-shine"/>
        <path d="M124 116c5 4 11 4 16 0" className="girl-mouth"/>
        <circle cx="102" cy="111" r="7" className="girl-blush"/>
        <circle cx="162" cy="111" r="7" className="girl-blush"/>
        <path d="M96 150c18-20 54-20 72 0l16 67H80l16-67Z" className="girl-dress"/>
        <path d="M107 147c2 15 47 15 50 0" className="girl-collar"/>
        <path d="M95 160 66 191M169 160l29 31" className="girl-limb"/>
        <path d="M106 216v24M158 216v24" className="girl-limb"/>
        <path d="M108 241H92M156 241h16" className="girl-shoe"/>
        <path d="m54 127 4 10 10 4-10 4-4 10-4-10-10-4 10-4 4-10Zm151 3 3 8 8 3-8 3-3 8-3-8-8-3 8-3 3-8Z" className="girl-star"/>
      </svg>
    </div>
  );
}

export function DoctorSticker() {
  return (
    <div className="doctor-sticker" aria-hidden="true">
      <svg viewBox="0 0 120 120">
        <circle cx="60" cy="60" r="54" className="doctor-bg"/>
        <circle cx="60" cy="46" r="27" className="doctor-face"/>
        <path d="M33 48c-2-27 13-41 29-41 18 0 32 17 29 42-9-13-19-19-30-19-10 0-20 6-28 18Z" className="doctor-hair"/>
        <circle cx="50" cy="47" r="3" className="doctor-eye"/>
        <circle cx="70" cy="47" r="3" className="doctor-eye"/>
        <path d="M54 59c4 3 8 3 12 0" className="doctor-mouth"/>
        <path d="M31 105c5-25 18-37 29-37s24 12 29 37" className="doctor-coat"/>
        <path d="M52 71 60 86l8-15" className="doctor-shirt"/>
        <path d="M60 86v18" className="doctor-line"/>
        <path d="M45 79c-9 4-10 16-3 21M75 79c9 4 10 16 3 21" className="doctor-line"/>
        <circle cx="42" cy="101" r="4" className="doctor-scope"/>
      </svg>
    </div>
  );
}
