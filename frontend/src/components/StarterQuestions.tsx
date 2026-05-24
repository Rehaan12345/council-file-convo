import "./StarterQuestions.css";

const STARTERS: Record<string, string[]> = {
  "17-0090": [
    "What is this legislation about?",
    "How could this affect South LA residents?",
    "What did the final ordinance actually change?",
    "When was this passed and who supported it?",
  ],
  "24-0011": [
    "What is this legislation about?",
    "What tree trimming services does this cover?",
    "How much funding was approved and where does it come from?",
    "Which council district does this affect?",
  ],
  "26-0900": [
    "What is this legislation about?",
    "Which neighborhoods or areas are affected by these street lighting districts?",
    "How does the property owner ballot process work?",
    "What happens if property owners vote no on the assessment?",
  ],
};

const DEFAULT_STARTERS = [
  "What is this legislation about?",
  "Who introduced this and when?",
  "What does this change or authorize?",
  "How does this affect residents?",
];

interface Props {
  legislation: string;
  onSelect: (question: string) => void;
}

export default function StarterQuestions({ legislation, onSelect }: Props) {
  const starters = STARTERS[legislation] ?? DEFAULT_STARTERS;

  return (
    <div className="starters">
      <p className="starters-label">Try asking:</p>
      <div className="starters-grid">
        {starters.map((q) => (
          <button key={q} className="starter-chip" onClick={() => onSelect(q)}>
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}
