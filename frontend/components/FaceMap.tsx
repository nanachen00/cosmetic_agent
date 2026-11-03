"use client";

import { useEffect, useRef, useState } from "react";

import type { FaceRegion } from "@/types";

const labels: Record<FaceRegion, string> = {
  t_zone: "T 字部位",
  left_cheek: "左臉頰",
  right_cheek: "右臉頰",
  chin: "下巴"
};

const faceImages = {
  open: "/face-map-v20261002/face-open.svg",
  closed: "/face-map-v20261002/face-open.svg",
  leftOpen: "/face-map-v20261002/face-open.svg",
  rightOpen: "/face-map-v20261002/face-open.svg"
} as const;

let preloadedFaceVariants: HTMLImageElement[] | null = null;

function preloadFaceVariants() {
 if (preloadedFaceVariants) return;
 preloadedFaceVariants = [faceImages.closed, faceImages.leftOpen, faceImages.rightOpen].map(
 (source) => {
 const image = new Image();
 image.decoding = "async";
 image.src = source;
 return image;
 }
 );
}

export function FaceMap({
  selected,
  onSelect,
  compact = false
}: {
  selected?: FaceRegion;
  onSelect?: (region: FaceRegion) => void;
  compact?: boolean;
}) {
  const clickable = Boolean(onSelect);
  const initialSelected = useRef(selected);
  const [hasChosenRegion, setHasChosenRegion] = useState(false);

 useEffect(() => {
 if (selected !== initialSelected.current) setHasChosenRegion(true);
 }, [selected]);

 useEffect(() => {
 if (!clickable || preloadedFaceVariants) return;

 if ("requestIdleCallback" in window) {
 const idleId = window.requestIdleCallback(preloadFaceVariants, { timeout: 2000 });
 return () => window.cancelIdleCallback(idleId);
 }

 const timer = globalThis.setTimeout(preloadFaceVariants, 1200);
 return () => globalThis.clearTimeout(timer);
 }, [clickable]);

  const faceImage = !hasChosenRegion
    ? faceImages.open
    : selected === "t_zone" || selected === "chin"
      ? faceImages.closed
      : selected === "left_cheek"
        ? faceImages.leftOpen
        : faceImages.rightOpen;

  const regionProps = (region: FaceRegion) => ({
    className: "face-region " + (selected === region ? "is-selected" : ""),
    role: clickable ? "button" : undefined,
    tabIndex: clickable ? 0 : undefined,
    "data-region": region,
    "aria-label": clickable ? "選擇" + labels[region] : undefined,
    "aria-pressed": clickable ? selected === region : undefined,
    onClick: clickable
      ? () => {
          setHasChosenRegion(true);
          onSelect?.(region);
        }
      : undefined,
    onKeyDown: clickable
      ? (event: React.KeyboardEvent<SVGElement>) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setHasChosenRegion(true);
            onSelect?.(region);
          }
        }
      : undefined
  });

  return (
    <div
      className={
        "face-map " +
        (compact ? "is-compact " : "") +
        (clickable ? "is-interactive" : "")
      }
    >
      <div className="face-map-stage">
        <img
          src={faceImage}
          alt="互動式臉部地圖"
          width={624}
          height={1039}
          draggable={false}
          decoding="async"
          loading="eager"
          fetchPriority="high"
        />
        <svg
          viewBox="0 0 624 1039"
          aria-label={clickable ? "選擇臉部區域" : "臉部區域示意"}
        >
          <path
            {...regionProps("t_zone")}
            d="M312 758C296 758 285 741 279 716C272 686 276 651 281 616C285 587 287 557 281 535C240 528 190 537 135 558C140 514 157 477 184 447C216 413 264 398 312 398C360 398 408 413 440 447C467 477 484 514 489 558C434 537 384 528 343 535C337 557 339 587 343 616C348 651 352 686 345 716C339 741 328 758 312 758Z"
          />
          <path
            {...regionProps("left_cheek")}
            d="M145 596C176 573 220 572 248 589C261 598 267 613 266 633C264 674 258 714 249 753C243 785 239 816 230 840C220 850 211 854 205 850C180 843 159 826 144 804C123 773 114 733 114 692C114 649 121 613 134 599C137 596 141 595 145 596Z"
          />
          <path
            {...regionProps("right_cheek")}
            d="M479 596C448 573 404 572 376 589C363 598 357 613 358 633C360 674 366 714 375 753C381 785 385 816 394 840C404 850 413 854 419 850C444 843 465 826 480 804C501 773 510 733 510 692C510 649 503 613 490 599C487 596 483 595 479 596Z"
          />
          <path
            {...regionProps("chin")}
            d="M245 787C267 778 289 774 312 774C335 774 357 778 379 787C389 792 393 803 390 819C384 850 365 875 335 889C322 896 302 896 289 889C259 875 240 850 234 819C231 803 235 792 245 787Z"
          />
        </svg>
      </div>
      {clickable ? <p className="face-map-help">直接點選彩色區塊</p> : null}
    </div>
  );
}
