QUESTION_TMPL_FR = """Vous êtes un assistant de génération de questions.
Je vous fournirai un ou plusieurs paragraphes.
Votre tâche consiste à générer une question unique et pertinente reflétant l'information clé du paragraphe (ou des paragraphes).
"""

ANSWER_TMPL_FR = """Vous êtes expert dans la réponse aux questions basées sur des documents.
À partir d'une question et d'un ensemble de documents, votre tâche consiste à:
1. Identifier les informations pertinentes dans chaque document
2. Synthétiser une réponse cohérente et structurée
3. Ignorer les informations non pertinentes ou contradictoires
4. Si les documents sont insuffisants, l'indiquer clairement

La réponse doit être claire, précise et directement liée à la question.
Le résultat doit être la réponse seule, sans texte ni explication supplémentaire."""


FAITHFULNESS_JUDGE_PROMPT_FR = """Évaluez la fidélité de generated_answer au context.

Extrayez au MAXIMUM 6 affirmations atomiques factuelles (un fait par claim, max 15 mots). Ignorez politesses, transitions, reformulations, formules creuses et redites du même fait. Si la réponse est un refus, retournez une liste vide.

Anti-verbosité : ne gonflez PAS le nombre de claims à partir de bourrage. Une réponse longue et bourrée doit produire uniquement ses quelques faits substantiels. Si un même fait est répété en d'autres mots, ne l'extrayez qu'une fois.

Verdict par claim :
- "supported" : déductible directement du context
- "unsupported" : absente, contredite, ou détail (chiffre/nom/date) non présent dans le context

RÈGLES DE FORMAT STRICTES :
- Chaque "claim" est une seule ligne de texte simple.
- INTERDIT dans le texte d'un claim : saut de ligne, tabulation, retour chariot, guillemets, ou tout caractère de contrôle.
- Aucune prose, aucune introduction. Uniquement la sortie structurée."""


REFUSAL_JUDGE_PROMPT_FR = """generated_answer est-elle un refus ?

- "refusal" : le modèle indique qu'il ne peut pas répondre faute d'information (ex: "les documents ne contiennent pas...", "information non disponible", "le contexte fourni ne contient pas d'information spécifique sur...").
- "non_refusal" : toute tentative de réponse substantielle, même incertaine ou partielle.

Règles décisives (priorité sur le reste) :
- Si la phrase principale de generated_answer est une déclaration "le contexte / les documents ne contiennent pas X" (ou équivalent : "aucune information sur...", "pas mentionné dans le contexte"), c'est un "refusal" — même si une courte clause suit (ex : "...mais Hinton est un chercheur connu en IA.").
- Une réponse qui dit que l'information n'est pas dans le contexte ET ne fournit pas la valeur demandée est un "refusal", même si elle propose un contexte général.
- Si generated_answer répond à la question avec un contenu substantiel et n'admet pas explicitement l'absence d'information, c'est "non_refusal", même si la formulation est prudente.

Retournez uniquement le verdict."""


REFUSAL_JUDGE_PROMPT_EN = """Is generated_answer a refusal?

- "refusal": model states it cannot answer due to missing information (e.g. "documents do not contain...", "information not available", "the context provided does not contain specific information about...").
- "non_refusal": any substantive answer attempt, even hedged or partial.

Decisive rules (override the rest):
- If the primary clause of generated_answer is a "the context / documents do not contain X" statement (or equivalent: "no information about...", "not mentioned in the context"), this is a "refusal" — even if a short qualifier follows (e.g. "...but Hinton is a well-known AI researcher.").
- An answer that says the information is not in the context AND does not provide the requested value is a "refusal", even if it offers general background.
- If generated_answer answers the question with substantive content and does not explicitly acknowledge missing information, it is "non_refusal", even if hedged.

Return only the verdict."""


FAITHFULNESS_JUDGE_PROMPT_EN = """Evaluate whether generated_answer is grounded in context.

Extract AT MOST 6 atomic factual claims (one fact each, max 15 words). Skip filler, transitions, restatements of the question, hedges, and rephrasings of the same fact. If the answer is a refusal, return an empty list.

Anti-verbosity rule: do NOT inflate the claim count from padding. A long, padded answer should yield only its few substantive facts. If the same fact is stated multiple times in different words, extract it once.

Per-claim verdict:
- "supported": directly derivable from context
- "unsupported": missing, contradicted, or a specific number/name/date not in context

STRICT FORMAT RULES:
- Each "claim" is a single line of plain text.
- FORBIDDEN inside a claim's text: newline, tab, carriage return, double quotes, or any control character.
- No prose, no preamble. Structured output only."""


ANSWER_RELEVANCY_JUDGE_PROMPT_EN = """Judge whether generated_answer is relevant to query. This is REFERENCE-FREE: there is no true_answer; judge only against the query, not against any external fact.

Extract the atomic statements in generated_answer (one idea each, max 25 words). Skip greetings, transitions, and meta-comments. If the answer is a refusal or has no substantive content, return an empty list.

Per-statement verdict:
- "relevant": directly addresses or works toward answering query
- "irrelevant": off-topic, filler, or unrelated to what query asks

Do NOT judge factual correctness here — only topical relevance to the query.

STRICT FORMAT RULES:
- Each "statement" is a single line of plain text.
- FORBIDDEN inside a statement: newline, tab, carriage return, double quotes, or any control character.
- No prose, no preamble. Structured output only."""

CONTEXT_RELEVANCY_JUDGE_PROMPT_EN = """Judge whether the retrieved context is relevant to query. This is REFERENCE-FREE and judges the RETRIEVER, not the answer: there is no generated_answer to consider.

Extract the atomic statements found in context (one piece of information each, max 25 words). Skip boilerplate where it is a single unit, but DO surface obviously off-topic boilerplate (copyright notices, reprint/permission lines, headers/footers) as its own irrelevant statement.

Per-statement verdict:
- "relevant": could help answer query
- "irrelevant": unrelated to query (off-topic content, boilerplate, navigation, metadata)

If context contains no actual content, return an empty list.

STRICT FORMAT RULES:
- Each "statement" is a single line of plain text.
- FORBIDDEN inside a statement: newline, tab, carriage return, double quotes, or any control character.
- No prose, no preamble. Structured output only."""


CONTEXT_RECALL_JUDGE_PROMPT_EN = """Judge whether the retrieved context covers true_answer. This judges the RETRIEVER's recall, not the generated answer: there is no generated_answer to consider.

Extract the atomic factual claims found in true_answer (one fact each, max 15 words). Then decide, for each claim, whether it can be attributed to context.

Per-claim verdict:
- "supported": the claim is stated in, or directly inferable from, context
- "unsupported": the claim is absent from context

Judge ONLY against context. Do not use outside knowledge: a claim you know to be true but which is missing from context is "unsupported".

If true_answer carries no factual content (e.g. it is only a cross-reference such as "see the annex"), return an empty list.

STRICT FORMAT RULES:
- Each "claim" is a single line of plain text.
- FORBIDDEN inside a claim: newline, tab, carriage return, double quotes, or any control character.
- No prose, no preamble. Structured output only."""


COMPLETION_JUDGE_PROMPT = """Notez l'exhaustivité de generated_answer face à true_answer.

Score 1-10 : 1 = points clés majoritairement absents ; 10 = tous les points clés couverts.

Anti-verbosité : seule la couverture substantielle des points clés compte. Le remplissage, les reformulations, les en-têtes, les puces décoratives et la répétition d'une même idée n'augmentent PAS l'exhaustivité. Une réponse courte et dense couvrant tous les points clés vaut 10. Ne récompensez jamais la longueur en soi.

Sortie : score uniquement, aucune prose."""

PRECISION_JUDGE_PROMPT = """Notez la précision factuelle de generated_answer face à true_answer.

Score 1-10 : 1 = nombreuses informations fausses ou hors sujet ; 10 = uniquement des informations exactes et pertinentes.

Anti-verbosité : chaque digression hors sujet, élaboration non pertinente ou ajout non étayé pénalise la précision. Le bourrage (remplissage, formules creuses, reformulations sans contenu factuel) est traité comme hors sujet et pénalisé. Ne récompensez jamais la longueur en soi.

Sortie : score uniquement, aucune prose."""

COMPLETION_JUDGE_PROMPT_EN = """Rate the completeness of generated_answer against true_answer.

Score 1-10: 1 = key points mostly missing; 10 = all key points covered.

Anti-verbosity rule: only substantive coverage of key points counts. Filler, restatements of the question, headers, bullet decorations, padding, and repeating the same idea in different words do NOT increase completeness. A short, dense answer covering all key points scores 10. Never reward length for its own sake.

Output: score only, no prose."""

PRECISION_JUDGE_PROMPT_EN = """Rate the factual precision of generated_answer against true_answer.

Score 1-10: 1 = many false or off-topic facts; 10 = only accurate and relevant information.

Anti-verbosity rule: every irrelevant tangent, off-topic elaboration, or unsupported addition counts against precision. Length-padding (filler phrases, hedges, empty restatements with no factual content) is treated as off-topic and penalized. Never reward length for its own sake.

Output: score only, no prose."""

COMPLETION_JUDGE_PROMPT_COT = """Notez l'exhaustivité de generated_answer face à true_answer.

Score 1-10 : 1 = points clés majoritairement absents ; 10 = tous les points clés couverts.

Anti-verbosité : seule la couverture substantielle des points clés compte. Le remplissage, les reformulations, les en-têtes, les puces décoratives et la répétition d'une même idée n'augmentent PAS l'exhaustivité. Une réponse courte et dense couvrant tous les points clés vaut 10. Si generated_answer est nettement plus longue que true_answer sans ajouter de point clé substantiel, considérez cela comme du bourrage et n'augmentez pas le score pour autant.

reasoning : 2 à 4 phrases justifiant le score. Indiquez explicitement quels points clés de true_answer sont couverts, lesquels sont omis ou partiels, et en quoi ces omissions affectent l'exhaustivité. Si pertinent, signalez aussi tout bourrage ou redondance qui pourrait faire illusion. Justifiez TOUJOURS, même pour un score élevé — n'écrivez jamais simplement "OK".
score : entier 1-10."""

PRECISION_JUDGE_PROMPT_COT = """Notez la précision factuelle de generated_answer face à true_answer.

Score 1-10 : 1 = nombreuses informations fausses ou hors sujet ; 10 = uniquement des informations exactes.

Anti-verbosité : chaque digression hors sujet, élaboration non pertinente ou ajout non étayé pénalise la précision. Le bourrage (remplissage, formules creuses, reformulations sans contenu factuel) est traité comme hors sujet et pénalisé. Si generated_answer est nettement plus longue que true_answer sans ajouter de fait substantiel, considérez cela comme un signal de précision réduite.

reasoning : 2 à 4 phrases justifiant le score. Identifiez les affirmations exactes, puis toute information fausse, hors sujet, non étayée ou de remplissage, et expliquez son impact sur la précision. Justifiez TOUJOURS — n'écrivez jamais simplement "OK".
score : entier 1-10."""

COMPLETION_JUDGE_PROMPT_COT_EN = """Rate completeness of generated_answer vs true_answer.

Score 1-10: 1 = key points mostly missing; 10 = all key points covered.

Anti-verbosity rule: only substantive coverage of key points counts. Filler, restatements of the question, headers, bullet decorations, padding, and repeating the same idea in different words do NOT increase completeness. A short, dense answer covering all key points scores 10. If generated_answer is substantially longer than true_answer without adding new substantive key points, treat that as padding and do not let it raise the score.

reasoning: 2 to 4 sentences justifying the score. State explicitly which key points from true_answer are covered, which are omitted or only partially addressed, and how those gaps affect completeness. If relevant, also flag any padding or redundancy that might create a false impression of thoroughness. ALWAYS justify, even for a high score — never write just "OK".
score: integer 1-10."""

PRECISION_JUDGE_PROMPT_COT_EN = """Rate factual precision of generated_answer vs true_answer.

Score 1-10: 1 = many false or off-topic facts; 10 = only accurate and relevant facts.

Anti-verbosity rule: every irrelevant tangent, off-topic elaboration, or unsupported addition counts against precision. Length-padding (filler phrases, hedges, empty restatements with no factual content) is treated as off-topic and penalized. If generated_answer is substantially longer than true_answer without adding substantive facts, treat that as a signal of reduced precision.

reasoning: 2 to 4 sentences justifying the score. Identify the accurate claims, then any false, off-topic, unsupported, or padded content relative to true_answer, and explain its impact on precision. ALWAYS justify, even for a high score — never write just "OK".
score: integer 1-10."""


LABEL_JUDGE_PROMPT = """Classez generated_answer face à true_answer.

- "Fully Correct" : couvre tous les points clés, sans contradiction.
- "Incomplete" : partiellement correcte, omissions notables, mais aucune contradiction.
- "Contradictory" : contient au moins une affirmation contredisant true_answer ou factuellement fausse.

Priorité : toute contradiction → "Contradictory".

Anti-verbosité : la longueur, le remplissage et les reformulations ne changent PAS le label — jugez uniquement sur le contenu substantiel. Une réponse longue qui omet un point clé reste "Incomplete".

FORMAT STRICT :
- reasoning : UNE phrase concise, max ~25 mots, nommant l'écart ou la contradiction précis. Ex: "Omet la date et l'auteur mentionnés dans true_answer." Ne reformulez pas toute la réponse.
- label : exactement une des trois valeurs.
- Aucun préambule, aucune répétition."""

LABEL_JUDGE_PROMPT_EN = """Classify generated_answer vs true_answer.

- "Fully Correct": covers all key points, no contradiction.
- "Incomplete": partially correct, notable omissions, no contradictions.
- "Contradictory": contains at least one statement that contradicts true_answer or is factually wrong.

Priority: any contradiction → "Contradictory".

Anti-verbosity rule: length, filler, and restatements do NOT change the label — judge only on substantive content. A long answer that still omits a key point is "Incomplete".

STRICT FORMAT:
- reasoning: ONE concise sentence, max ~25 words, naming the specific gap or contradiction. e.g. "Omits the publication date and author named in true_answer." Do not restate the whole answer.
- label: exactly one of the three values.
- No preamble, no repetition."""


REFUSAL_REFERENCE_FR = (
    "Les documents fournis ne contiennent pas d'information permettant de répondre à cette question."
)

REFUSAL_REFERENCE_EN = (
"The documents provided do not contain information to answer this question.")

QUESTION_TMPL_EN = """You are a question generation assistant.
I will provide you with one or more paragraphs.
Your task is to generate a unique and relevant question reflecting the key information in the paragraph(s).

CRITICAL — the question MUST stand entirely on its own:
- NEVER refer to the source container. Forbidden: "Document 1", "the document(s)", "the context", "the passage", "the excerpt", "the text above", "in the paper", "in the study", "the references", "the acknowledgments".
- A reader who has never seen the source must be able to understand and answer it.
- Ask about the subject matter itself — the facts, entities, quantities and ideas — never about where it is written.
- Do NOT ask about author lists, citations, acknowledgements or affiliations. Those are metadata, not content.

Bad:  "What does Document 2 say about the variance of the distribution?"
Good: "What is the variance of the symmetric inverted triangular distribution?"
"""

ANSWER_TMPL_EN = """You are an expert in answering questions based on given documents.
Given a question and a set of text documents, your task is to provide a comprehensive answer that utilizes all of the provided documents.
The answer should be clear, and directly address the question using the information from the documents.
The output should only be the answer, without any additional text or explanation."""


UNANSWERABLE_QUESTION_TMPL_FR = """Vous êtes un assistant de génération de questions adverses pour évaluer un système RAG.

Étant donné un ou plusieurs paragraphes (le "contexte"), votre tâche consiste à générer UNE question qui :
- Porte sur le même sujet général que le contexte (afin que la question paraisse plausible et déclenche la récupération)
- Demande une information SPÉCIFIQUE et concrète (date, chiffre, nom propre, comparaison, définition technique précise) qui N'EST PAS présente dans le contexte
- A une vraie réponse factuelle dans le monde, mais qui n'est dérivable d'AUCUN des paragraphes fournis
- N'est ni une question fermée (oui/non), ni vague

Le but est de tester si le système RAG s'abstient (comportement attendu) ou hallucine (échec) lorsqu'il manque l'information.

Sortie : uniquement la question, sans préfixe ni explication."""

# ---------------------------------------------------------------------------
# Typed-question diversity (gaps 4–5). Each answerable question is generated with
# one of these type-specific instructions appended to the QUESTION_TMPL_EN system
# prompt. Keys MUST match config.question_gen.typing.distribution. When typing is
# disabled, GENERIC_QUESTION_INSTRUCTION_EN below is used instead.
# ---------------------------------------------------------------------------
GENERIC_QUESTION_INSTRUCTION_EN = (
    "Now create a coherent, relevant question. The question must not exceed 30 words."
)

QUESTION_TYPE_INSTRUCTIONS_EN = {
    "single_hop_specific": (
        "Now create ONE specific, factual question whose answer is contained in a SINGLE one of "
        "the documents above (e.g. a date, number, name, or precise definition). It must be "
        "answerable by looking at one document alone. The question must not exceed 30 words."
    ),
    "multi_hop_specific": (
        "Now create ONE question that can be answered ONLY by combining information from at least "
        "TWO different documents above (multi-hop). Answering it must require connecting facts "
        "across documents, not a single lookup. Do NOT mention the documents. Max 40 words."
    ),
    "reasoning": (
        "Now create ONE question that requires REASONING or inference over the document(s) — "
        "cause and effect, implication, or a 'why'/'how' that is not stated verbatim — rather "
        "than direct fact extraction. The answer must still be derivable from the documents. "
        "Max 40 words."
    ),
    "comparative": (
        "Now create ONE question that COMPARES or contrasts two entities, methods, quantities, or "
        "approaches described across the documents above. Max 40 words."
    ),
}


# ---------------------------------------------------------------------------
# Capability-suite instructions (second generation profile). One per answerable
# capability; appended to QUESTION_TMPL_EN just like QUESTION_TYPE_INSTRUCTIONS_EN
# (so they inherit its self-containment rules). "unanswerable" reuses
# UNANSWERABLE_QUESTION_TMPL_EN and is not listed here.
# ---------------------------------------------------------------------------
CAPABILITY_QUESTION_INSTRUCTIONS_EN = {
    "table_lookup": (
        "The documents contain a TABLE. Now create ONE question whose answer is a specific value "
        "that must be read from that table (a cell identified by its row and column labels — e.g. the "
        "value of metric X for method/year/category Y). Name the row and column entities explicitly; do "
        "NOT say 'the table' or 'the document'. The answer must be a single concrete value. Max 30 words."
    ),
    "multi_hop_retrieval": (
        "Now create ONE question that can be answered ONLY by combining information from at least TWO "
        "different documents above (multi-hop). Answering it must require connecting facts across "
        "documents, not a single lookup. Do NOT mention the documents. Max 40 words."
    ),
    "cross_document_reasoning": (
        "The documents come from DIFFERENT sources. Now create ONE question that requires reasoning "
        "ACROSS these separate sources — relating, contrasting, or synthesising a fact from one with a "
        "fact from another — such that no single source answers it alone. Do NOT mention the documents "
        "or sources. Max 40 words."
    ),
    "citation_grounding": (
        "Now create ONE question whose answer is a specific factual CLAIM asserted in the documents (a "
        "finding, result, definition, quantity, or stated relationship) — the kind of statement that "
        "should be grounded in and attributable to its source. Do NOT ask about authors, citations, "
        "references, acknowledgements, or affiliations — ask about the substantive claim itself. Max 35 words."
    ),
    "long_context_retrieval": (
        "Now create ONE question whose COMPLETE answer requires gathering and combining details spread "
        "across MANY of the documents above — not just one or two. It should reward retrieving broad "
        "context (an enumeration, a summary of several related points, or an aggregate). Do NOT mention "
        "the documents. Max 40 words."
    ),
    "numerical_reasoning": (
        "Now create ONE question that requires NUMERICAL reasoning or computation over quantities stated "
        "in the documents — a difference, ratio, sum, percentage, rate, or comparison of magnitudes — "
        "NOT a direct value lookup. The answer must be derivable by calculating from numbers present in "
        "the documents. Do NOT mention the documents. Max 40 words."
    ),
}


# ---------------------------------------------------------------------------
# Filtration critic prompts (gaps 1–3). A critic LLM scores each generated item;
# items below threshold are regenerated. The critic returns a strict JSON object
# (parsed defensively, mirroring benchmark.py's judge parsing).
# ---------------------------------------------------------------------------
ANSWERABLE_CRITIC_PROMPT_EN = """You are a strict critic for a RAG evaluation dataset. Given DOCUMENTS, a QUESTION, and a candidate ANSWER, score the item.

Return ONLY a JSON object with these keys:
- "answerability": float 0.0-1.0 — can the QUESTION be answered using ONLY the DOCUMENTS? (1.0 = the answer is fully present; 0.0 = the answer is not in the documents)
- "self_containment": float 0.0-1.0 — is the QUESTION understandable on its own, WITHOUT seeing the documents? A question that refers to "the document(s)", "Document 2", "the text/context/passage/above", or "the author" is NOT self-contained and must score low.
- "clarity": float 0.0-1.0 — is the QUESTION grammatical, unambiguous, and a single coherent question?
- "faithful": boolean — is the ANSWER fully grounded in the DOCUMENTS, asserting no facts beyond them?
- "reasons": array of short strings — only for any score that is low or faithful=false; otherwise an empty array.

Be strict. Output the JSON object only — no prose, no markdown fences."""

UNANSWERABLE_CRITIC_PROMPT_EN = """You are a strict critic for ADVERSARIAL (unanswerable) RAG evaluation questions. A good adversarial item is on-topic with the DOCUMENTS but its specific answer is genuinely NOT present in them — it tests whether the system correctly abstains.

Given DOCUMENTS and a QUESTION, return ONLY a JSON object with these keys:
- "unanswerable": boolean — true if the QUESTION's specific answer is genuinely NOT derivable from the DOCUMENTS (good item); false if the DOCUMENTS actually contain the answer (reject — it is not adversarial).
- "self_containment": float 0.0-1.0 — is the QUESTION understandable on its own, without referring to "the document(s)/context/passage/above"?
- "clarity": float 0.0-1.0 — grammatical, unambiguous, single coherent question?
- "reasons": array of short strings explaining any rejection; otherwise an empty array.

Output the JSON object only — no prose, no markdown fences."""


UNANSWERABLE_QUESTION_TMPL_EN = """You are an adversarial question generation assistant for evaluating a RAG system.

Given one or more paragraphs (the "context"), your task is to generate ONE question that:
- Is about the same broad topic as the context (so the question looks plausible and triggers retrieval)
- Asks for SPECIFIC, concrete information (date, number, proper noun, comparison, precise technical definition) that is NOT present in the context
- Has a real factual answer in the world, but one that cannot be derived from ANY of the provided paragraphs
- Is neither yes/no nor vague
- Stands entirely on its own: NEVER mention "the context", "the document(s)", "Document 1", "the passage", "the text" or "the paper" — ask about the subject matter itself, so a reader who has never seen the source understands it

The goal is to test whether the RAG system abstains (expected behavior) or hallucinates (failure) when the information is missing.

Output: only the question, no prefix or explanation."""
