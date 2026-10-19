import type { Metadata } from "next";
import { GeneratorPrototype } from "@/components/generator/GeneratorPrototype";
import { pageMetadata } from "@/lib/site";

export const metadata: Metadata = pageMetadata({
  title: "Explore names by meaning",
  description:
    "Type a word, choose the meaning you want, and see names and words that carry it in 21 languages, grouped by how closely they match.",
  path: "/generate",
});

export default function GeneratePage() {
  return <GeneratorPrototype />;
}
