import { defineCollection, z } from "astro:content";

// All editable copy lives in this single data collection.
// Decap CMS edits the JSON at src/content/landing/main.json via the
// admin UI; Astro reads it here and the page renders from it.
const landing = defineCollection({
  type: "data",
  schema: z.object({
    meta: z.object({
      title: z.string(),
      description: z.string(),
    }),
    links: z.object({
      github: z.string(),
      results: z.string(),
      roadmap: z.string(),
      contact_email: z.string(),
      pilot_email: z.string(),
      linkedin: z.string().optional(),
    }),
    nav: z.object({
      brand: z.string(),
      items: z.array(z.object({
        label: z.string(),
        href: z.string(),
      })),
    }),
    hero: z.object({
      eyebrow: z.string(),
      headline_line1: z.string(),
      headline_highlight: z.string(),
      headline_line2: z.string(),
      tagline: z.string(),
      primary_cta: z.string(),
      secondary_cta: z.string(),
      stats: z.array(z.object({
        label: z.string(),
        value: z.string(),
        sublabel: z.string().optional(),
      })),
    }),
    problem: z.object({
      eyebrow: z.string(),
      heading: z.string(),
      paragraphs: z.array(z.string()),
      pullquote: z.string(),
    }),
    how_it_works: z.object({
      eyebrow: z.string(),
      heading: z.string(),
      intro: z.string(),
      diagram: z.string(),
      signal_path_title: z.string(),
      signal_path_steps: z.array(z.string()),
      prior_art_title: z.string(),
      prior_art: z.string(),
    }),
    proof: z.object({
      eyebrow: z.string(),
      heading_line1: z.string(),
      heading_line2: z.string(),
      paragraphs: z.array(z.string()),
      results_link_text: z.string(),
      placeholder_caption: z.string(),
      comparison_rows: z.array(z.object({
        system: z.string(),
        mae: z.string(),
        hardware: z.string(),
        per_node_cost: z.string(),
        highlight: z.boolean().default(false),
      })),
    }),
    roadmap: z.object({
      eyebrow: z.string(),
      heading: z.string(),
      intro: z.string(),
      steps: z.array(z.object({
        date: z.string().optional(),
        title: z.string(),
        body: z.string(),
      })),
      live_manifest_text: z.string(),
    }),
    hospitals: z.object({
      eyebrow: z.string(),
      heading: z.string(),
      intro_paragraphs: z.array(z.string()),
      pilot_bullets: z.array(z.string()),
      cta: z.string(),
    }),
    footer: z.object({
      disclaimer_title: z.string(),
      disclaimer_body: z.string(),
      copyright: z.string(),
    }),
  }),
});

// Subpage schema: each page has metadata, a title, an optional intro
// paragraph, and an ordered list of sections. Each section is an h2 with
// a list of typed content blocks (paragraph, subheading, bulleted_list,
// ordered_list, table). Decap CMS edits these JSON files via the admin UI.
const block = z.object({
  type: z.enum([
    "paragraph",
    "subheading",
    "bulleted_list",
    "ordered_list",
    "table",
  ]),
  text: z.string().optional(),
  items: z.array(z.string()).optional(),
  headers: z.array(z.string()).optional(),
  rows: z.array(z.array(z.string())).optional(),
});

const subpageSchema = z.object({
  meta: z.object({
    title: z.string(),
    description: z.string(),
  }),
  title: z.string(),
  intro: z.string().optional(),
  sections: z.array(
    z.object({
      id: z.string().optional(),
      eyebrow: z.string().optional(),
      heading: z.string(),
      blocks: z.array(block),
    }),
  ),
});

const results = defineCollection({ type: "data", schema: subpageSchema });
const roadmap = defineCollection({ type: "data", schema: subpageSchema });
const privacy = defineCollection({ type: "data", schema: subpageSchema });
const terms = defineCollection({ type: "data", schema: subpageSchema });

export const collections = { landing, results, roadmap, privacy, terms };
