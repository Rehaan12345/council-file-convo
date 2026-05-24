import "./StarterQuestions.css";

const DEFAULT_STARTERS = [
  "What is this legislation about?",
  "Who introduced this and when?",
  "What does this change or authorize?",
  "How does this affect residents?",
];

interface Props {
  starters?: string[];
  onSelect: (question: string) => void;
}

export default function StarterQuestions({ starters, onSelect }: Props) {
  const questions = starters && starters.length > 0 ? starters : DEFAULT_STARTERS;

  return (
    <div className="starters">
      <p className="starters-label">Try asking:</p>
      <div className="starters-grid">
        {questions.map((q) => (
          <button key={q} className="starter-chip" onClick={() => onSelect(q)}>
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}
