/** Small, shared outline icons for the console workbench. */
const paths = {
  play: "M5 3l12 7-12 7z",
  results: "M3 2v16h15 M7 14V9 M11 14V5 M15 14v-3",
  workflow: "M2 2h6v5H2z M12 13h6v5h-6z M5 7v8h7 M8 4h7v9",
  plug: "M6 2v5 M14 2v5 M4 7h12v3a6 6 0 0 1-12 0z M10 16v3",
  gear: "M8 2h4l1 3 3 1 2 3-2 3v3l-3 2-3-1-3 1-3-2v-3L2 9l2-3 3-1z M13 10a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  system: "M2 3h16v13H2z M5 11l3-4 3 5 4-6 M7 19h6 M10 16v3",
  settings: "M3 5h14 M3 10h14 M3 15h14 M7 3v4 M13 8v4 M8 13v4",
  profile: "M3 3h10l4 4v10H3z M12 3v5h5 M6 11h8 M6 14h5",
  search: "M14 8a6 6 0 1 1-12 0 6 6 0 0 1 12 0z M12.5 12.5L18 18",
  plus: "M10 3v14 M3 10h14",
  refresh: "M17 7a7 7 0 1 0 0 6 M17 2v5h-5",
  check: "M3 10l4 4L17 4",
  chevron: "M7 4l6 6-6 6",
  signal: "M1 10h4l3-7 4 14 3-7h4",
  folder: "M2 5h6l2 2h8v10H2z",
  copy: "M7 7h10v11H7z M13 7V2H2v11h5",
  close: "M5 5l10 10 M15 5L5 15",
} as const;

export function WorkbenchIcon({ name }: { name: keyof typeof paths }) {
  return <svg className="workbench-icon" viewBox="0 0 20 20" fill="none"
    stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"
    aria-hidden="true" focusable="false"><path d={paths[name]} /></svg>;
}
