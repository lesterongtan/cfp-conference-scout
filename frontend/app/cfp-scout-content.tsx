"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const EVENT_FORMAT_OPTIONS = [
  { value: "virtual", label: "Virtual" },
  { value: "in_person", label: "In-person" },
  { value: "hybrid", label: "Hybrid" },
];

const DEFAULT_MIN_DAYS_OUT = 180;
const DEFAULT_MAX_DAYS_OUT = 365;

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

// Raised from 50: at 50, the events lane (many more candidates) filled the
// cap before slower directory-lane results (e.g. 10times via Apify) ever
// landed in the truncated output. This is a sanity ceiling, not a target.
const MAX_RESULTS = 200;
const POLL_INTERVAL_MS = 3000;

interface CfpResult {
  name: string;
  url: string;
  lane: string;
  found_via: string;
  found_at: string;
  cfp_status: string;
  description: string;
  location: string;
  event_date: string;
  date_confidence: string;
  pay: string;
  contact_email: string;
  contact_name: string;
  contact_role: string;
  contact_source: string;
  submission_form_url: string;
  event_type: string;
  event_format: string;
  venue_name: string;
  promoter_name: string;
  promoter_website: string;
}

type RunStatus = "idle" | "running" | "completed" | "error";

export function CfpScoutContent() {
  const [keywordsInput, setKeywordsInput] = useState("");
  const [status, setStatus] = useState<RunStatus>("idle");
  const [results, setResults] = useState<CfpResult[]>([]);
  const [urlsFound, setUrlsFound] = useState(0);
  const [directoryItemsFound, setDirectoryItemsFound] = useState(0);
  const [confsTechItemsFound, setConfsTechItemsFound] = useState(0);
  const [error, setError] = useState("");
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  // Profile — Phase 1 discovery inputs (niches/topics/expertise/audiences/
  // geography/date window/formats/exclusions). Purely used to shape query
  // generation and filtering, never for scoring/ranking candidates.
  const [showProfile, setShowProfile] = useState(false);
  const [primaryNiche, setPrimaryNiche] = useState("");
  const [secondaryNiches, setSecondaryNiches] = useState("");
  const [speakingTopics, setSpeakingTopics] = useState("");
  const [expertiseKeywords, setExpertiseKeywords] = useState("");
  const [audiences, setAudiences] = useState("");
  const [geography, setGeography] = useState("");
  const [countryCode, setCountryCode] = useState("WW");
  const [minDaysOut, setMinDaysOut] = useState(String(DEFAULT_MIN_DAYS_OUT));
  const [maxDaysOut, setMaxDaysOut] = useState(String(DEFAULT_MAX_DAYS_OUT));
  const [eventFormats, setEventFormats] = useState<string[]>([]);
  const [exclusions, setExclusions] = useState("");

  useEffect(() => {
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
    };
  }, []);

  const pollStatus = (runId: string) => {
    pollTimer.current = setInterval(async () => {
      try {
        const res = await fetch(`/api/cfp-scout/status/${runId}`, {
          cache: "no-store",
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data?.error || "Status check failed");
        }
        if (data.status === "completed") {
          setResults(data.results || []);
          setUrlsFound(data.urls_found || 0);
          setDirectoryItemsFound(data.directory_items_found || 0);
          setConfsTechItemsFound(data.confs_tech_items_found || 0);
          setStatus("completed");
          if (pollTimer.current) clearInterval(pollTimer.current);
        } else if (data.status === "error") {
          setError(data.error || "Scout run failed");
          setStatus("error");
          if (pollTimer.current) clearInterval(pollTimer.current);
        }
      } catch (err) {
        console.error(err);
        setError(err instanceof Error ? err.message : "Status check failed");
        setStatus("error");
        if (pollTimer.current) clearInterval(pollTimer.current);
      }
    }, POLL_INTERVAL_MS);
  };

  const runScout = async () => {
    const keywords = splitList(keywordsInput);

    const secondaryNichesList = splitList(secondaryNiches);
    const speakingTopicsList = splitList(speakingTopics);
    const expertiseKeywordsList = splitList(expertiseKeywords);
    const audiencesList = splitList(audiences);
    const exclusionsList = splitList(exclusions);

    const hasProfileTerms =
      primaryNiche.trim() ||
      secondaryNichesList.length > 0 ||
      speakingTopicsList.length > 0 ||
      expertiseKeywordsList.length > 0 ||
      audiencesList.length > 0;

    if (keywords.length === 0 && !hasProfileTerms) {
      setError(
        "Enter at least one keyword, or fill in a niche/topic/expertise/audience in the profile.",
      );
      return;
    }

    const hasProfileValues =
      hasProfileTerms ||
      geography.trim() ||
      countryCode.trim() !== "WW" ||
      Number(minDaysOut) !== DEFAULT_MIN_DAYS_OUT ||
      Number(maxDaysOut) !== DEFAULT_MAX_DAYS_OUT ||
      eventFormats.length > 0 ||
      exclusionsList.length > 0;

    const profile = hasProfileValues
      ? {
          primary_niche: primaryNiche.trim(),
          secondary_niches: secondaryNichesList,
          speaking_topics: speakingTopicsList,
          expertise_keywords: expertiseKeywordsList,
          audiences: audiencesList,
          geography: geography.trim(),
          country_code: countryCode.trim() || "WW",
          min_days_out: Number(minDaysOut) || DEFAULT_MIN_DAYS_OUT,
          max_days_out: Number(maxDaysOut) || DEFAULT_MAX_DAYS_OUT,
          event_formats: eventFormats,
          exclusions: exclusionsList,
        }
      : undefined;

    setError("");
    setResults([]);
    setUrlsFound(0);
    setDirectoryItemsFound(0);
    setConfsTechItemsFound(0);
    setStatus("running");

    try {
      const res = await fetch("/api/cfp-scout/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keywords, max_results: MAX_RESULTS, profile }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data?.error || "Failed to start scout");
      }
      pollStatus(data.run_id);
    } catch (err) {
      console.error(err);
      setError(err instanceof Error ? err.message : "Failed to start scout");
      setStatus("error");
    }
  };

  const toggleEventFormat = (value: string) => {
    setEventFormats((prev) =>
      prev.includes(value) ? prev.filter((v) => v !== value) : [...prev, value],
    );
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="pt-6 space-y-4">
          <div className="space-y-2">
            <label className="text-sm font-medium">
              Keywords{" "}
              <span className="text-muted-foreground font-normal">
                (comma-separated, e.g. "AI, healthcare, marketing")
              </span>
            </label>
            <Input
              value={keywordsInput}
              onChange={(e) => setKeywordsInput(e.target.value)}
              placeholder="AI, healthcare, marketing"
              disabled={status === "running"}
              onKeyDown={(e) => {
                if (e.key === "Enter" && status !== "running") runScout();
              }}
            />
          </div>

          <button
            type="button"
            onClick={() => setShowProfile((v) => !v)}
            className="text-sm font-medium text-primary hover:underline"
          >
            {showProfile ? "Hide profile ▲" : "Advanced: speaker profile ▼"}
          </button>

          {showProfile && (
            <div className="space-y-4 rounded-md border p-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <label className="text-sm font-medium">Primary niche</label>
                  <Input
                    value={primaryNiche}
                    onChange={(e) => setPrimaryNiche(e.target.value)}
                    placeholder="e.g. healthcare"
                    disabled={status === "running"}
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-sm font-medium">
                    Secondary niches{" "}
                    <span className="text-muted-foreground font-normal">(comma-separated)</span>
                  </label>
                  <Input
                    value={secondaryNiches}
                    onChange={(e) => setSecondaryNiches(e.target.value)}
                    placeholder="e.g. fintech, edtech"
                    disabled={status === "running"}
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-sm font-medium">
                    Speaking topics{" "}
                    <span className="text-muted-foreground font-normal">(comma-separated)</span>
                  </label>
                  <Textarea
                    value={speakingTopics}
                    onChange={(e) => setSpeakingTopics(e.target.value)}
                    placeholder="e.g. AI adoption, patient experience"
                    disabled={status === "running"}
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-sm font-medium">
                    Expertise keywords{" "}
                    <span className="text-muted-foreground font-normal">(comma-separated)</span>
                  </label>
                  <Textarea
                    value={expertiseKeywords}
                    onChange={(e) => setExpertiseKeywords(e.target.value)}
                    placeholder="e.g. clinical operations, ML"
                    disabled={status === "running"}
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-sm font-medium">
                    Audiences{" "}
                    <span className="text-muted-foreground font-normal">(comma-separated)</span>
                  </label>
                  <Input
                    value={audiences}
                    onChange={(e) => setAudiences(e.target.value)}
                    placeholder="e.g. hospital executives, CTOs"
                    disabled={status === "running"}
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-sm font-medium">
                    Geography{" "}
                    <span className="text-muted-foreground font-normal">
                      (free text, appended to search queries)
                    </span>
                  </label>
                  <Input
                    value={geography}
                    onChange={(e) => setGeography(e.target.value)}
                    placeholder="e.g. Europe, United States"
                    disabled={status === "running"}
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-sm font-medium">
                    Directory country code{" "}
                    <span className="text-muted-foreground font-normal">
                      ("WW" = worldwide)
                    </span>
                  </label>
                  <Input
                    value={countryCode}
                    onChange={(e) => setCountryCode(e.target.value.toUpperCase())}
                    placeholder="WW"
                    disabled={status === "running"}
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-sm font-medium">Date window (days out)</label>
                  <div className="flex items-center gap-2">
                    <Input
                      type="number"
                      min={0}
                      value={minDaysOut}
                      onChange={(e) => setMinDaysOut(e.target.value)}
                      disabled={status === "running"}
                    />
                    <span className="text-muted-foreground text-sm">to</span>
                    <Input
                      type="number"
                      min={0}
                      value={maxDaysOut}
                      onChange={(e) => setMaxDaysOut(e.target.value)}
                      disabled={status === "running"}
                    />
                  </div>
                </div>
                <div className="space-y-2">
                  <label className="text-sm font-medium">Event formats</label>
                  <div className="flex flex-wrap gap-3 pt-1">
                    {EVENT_FORMAT_OPTIONS.map((opt) => (
                      <label
                        key={opt.value}
                        className="flex items-center gap-1.5 text-sm font-normal"
                      >
                        <input
                          type="checkbox"
                          checked={eventFormats.includes(opt.value)}
                          onChange={() => toggleEventFormat(opt.value)}
                          disabled={status === "running"}
                        />
                        {opt.label}
                      </label>
                    ))}
                  </div>
                </div>
                <div className="space-y-2 sm:col-span-2">
                  <label className="text-sm font-medium">
                    Exclusions{" "}
                    <span className="text-muted-foreground font-normal">
                      (comma-separated terms to drop — matched against name, description,
                      promoter, domain)
                    </span>
                  </label>
                  <Input
                    value={exclusions}
                    onChange={(e) => setExclusions(e.target.value)}
                    placeholder="e.g. pharma, timeshare"
                    disabled={status === "running"}
                  />
                </div>
              </div>
            </div>
          )}

          <div className="flex items-center gap-3">
            <Button onClick={runScout} disabled={status === "running"}>
              {status === "running" ? (
                <>
                  <Spinner className="mr-2" />
                  Scouting…
                </>
              ) : (
                "Run Scout"
              )}
            </Button>
            <span className="text-xs text-muted-foreground">
              Capped at {MAX_RESULTS} results while testing.
            </span>
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </CardContent>
      </Card>

      {status === "completed" && (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            Found {results.length} active conference
            {results.length === 1 ? "" : "s"} (6–12 months out, CFP not
            closed) from {urlsFound} candidate URL{urlsFound === 1 ? "" : "s"}
            {directoryItemsFound > 0
              ? ` and ${directoryItemsFound} directory listing${directoryItemsFound === 1 ? "" : "s"}`
              : ""}
            {confsTechItemsFound > 0
              ? ` and ${confsTechItemsFound} Confs.tech listing${confsTechItemsFound === 1 ? "" : "s"}`
              : ""}
            .
          </p>
          <Card>
            <CardContent className="p-0">
              <Table className="table-fixed text-xs">
                <colgroup>
                  <col className="w-[22%]" />
                  <col className="w-[7%]" />
                  <col className="w-[10%]" />
                  <col className="w-[14%]" />
                  <col className="w-[8%]" />
                  <col className="w-[13%]" />
                  <col className="w-[13%]" />
                  <col className="w-[13%]" />
                </colgroup>
                <TableHeader>
                  <TableRow>
                    <TableHead className="whitespace-normal">
                      Conference
                    </TableHead>
                    <TableHead className="whitespace-normal">Type</TableHead>
                    <TableHead className="whitespace-normal">
                      CFP Status
                    </TableHead>
                    <TableHead className="whitespace-normal">
                      When / Where
                    </TableHead>
                    <TableHead className="whitespace-normal">Pay</TableHead>
                    <TableHead className="whitespace-normal">
                      Promoter
                    </TableHead>
                    <TableHead className="whitespace-normal">
                      Coordinator
                    </TableHead>
                    <TableHead className="whitespace-normal">Apply</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {results.map((r, i) => (
                    <TableRow key={`${r.url}-${i}`} className="align-top">
                      <TableCell className="whitespace-normal break-words py-2">
                        <div className="flex flex-wrap items-center gap-1">
                          <a
                            href={r.url}
                            target="_blank"
                            rel="noreferrer"
                            className="font-medium text-primary hover:underline"
                          >
                            {r.name}
                          </a>
                          {r.lane === "directories" && (
                            <Badge
                              variant="secondary"
                              className="shrink-0 text-[10px]"
                            >
                              Directory
                            </Badge>
                          )}
                          {r.lane === "confs_tech" && (
                            <Badge
                              variant="secondary"
                              className="shrink-0 text-[10px]"
                            >
                              Confs.tech
                            </Badge>
                          )}
                        </div>
                        <div className="mt-0.5 text-muted-foreground">
                          {r.found_at} · via {r.found_via}
                        </div>
                        {r.description && (
                          <p className="mt-1 text-muted-foreground">
                            {r.description}
                          </p>
                        )}
                      </TableCell>
                      <TableCell className="whitespace-normal break-words py-2 text-muted-foreground">
                        <div>{r.event_type || "—"}</div>
                        {r.event_format && r.event_format !== "unknown" && (
                          <div className="text-[10px] italic">{r.event_format}</div>
                        )}
                      </TableCell>
                      <TableCell className="whitespace-normal py-2">
                        <Badge
                          variant={
                            r.cfp_status.startsWith("Open")
                              ? "default"
                              : "outline"
                          }
                          className="whitespace-normal text-[10px]"
                        >
                          {r.cfp_status}
                        </Badge>
                      </TableCell>
                      <TableCell className="whitespace-normal break-words py-2 text-muted-foreground">
                        <div>{r.event_date || "—"}</div>
                        {r.event_date && r.date_confidence === "text" && (
                          <div className="italic">from page text</div>
                        )}
                        {r.venue_name && <div>{r.venue_name}</div>}
                        <div>{r.location || "—"}</div>
                      </TableCell>
                      <TableCell className="whitespace-normal break-words py-2 text-muted-foreground">
                        {r.pay || "—"}
                      </TableCell>
                      <TableCell className="whitespace-normal break-words py-2">
                        {r.promoter_name ? (
                          r.promoter_website ? (
                            <a
                              href={r.promoter_website}
                              target="_blank"
                              rel="noreferrer"
                              className="text-primary hover:underline"
                            >
                              {r.promoter_name}
                            </a>
                          ) : (
                            r.promoter_name
                          )
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell className="whitespace-normal break-words py-2">
                        {r.contact_name || r.contact_email ? (
                          <div className="space-y-0.5">
                            {r.contact_name && (
                              <div className="flex flex-wrap items-center gap-1">
                                <span className="font-medium">{r.contact_name}</span>
                                {r.contact_source === "ai" && (
                                  <Badge
                                    variant="secondary"
                                    className="shrink-0 text-[10px]"
                                    title="Found via AI extraction, not a direct scrape"
                                  >
                                    AI
                                  </Badge>
                                )}
                              </div>
                            )}
                            {r.contact_role && (
                              <div className="text-muted-foreground text-[10px] italic">
                                {r.contact_role}
                              </div>
                            )}
                            {r.contact_email && (
                              <div className="flex flex-wrap items-center gap-1">
                                <a
                                  href={`mailto:${r.contact_email}`}
                                  className="text-primary hover:underline"
                                >
                                  {r.contact_email}
                                </a>
                                {!r.contact_name && r.contact_source === "ai" && (
                                  <Badge
                                    variant="secondary"
                                    className="shrink-0 text-[10px]"
                                    title="Found via AI extraction, not a direct scrape"
                                  >
                                    AI
                                  </Badge>
                                )}
                              </div>
                            )}
                          </div>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell className="whitespace-normal break-words py-2">
                        {r.submission_form_url ? (
                          <a
                            href={r.submission_form_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-primary hover:underline"
                          >
                            Apply →
                          </a>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                  {results.length === 0 && (
                    <TableRow>
                      <TableCell
                        colSpan={8}
                        className="text-center text-muted-foreground py-8"
                      >
                        No conferences found for these keywords.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
