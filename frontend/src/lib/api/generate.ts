import type {
  GenerationFlavor,
  NameResult,
} from "@/features/generator/types";

import { apiUrl } from "./base";

interface GenerateNamesRequest {
  meanings: string[];
  language: string | null;
  minLength: number;
  maxLength: number;
  flavor: GenerationFlavor;
}

export async function generateNames(
  request: GenerateNamesRequest
): Promise<NameResult[]> {
  const response = await fetch(apiUrl("/generate"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    throw new Error(`Backend returned status ${response.status}`);
  }

  return response.json();
}