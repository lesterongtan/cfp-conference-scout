import { CfpScoutContent } from "./cfp-scout-content";
import { Badge } from "@/components/ui/badge";

const BUCKETS = [
  { label: "Events", description: "Conferences, summits, forums, annual meetings & related programs" },
  { label: "Coordinators", description: "The people responsible for programming, speakers, and event operations" },
  { label: "Promoters", description: "Organizations and organizers producing recurring or upcoming programs" },
  { label: "Venues", description: "Locations that regularly host niche-relevant conferences and programs" },
  { label: "Directories", description: "Conference, industry, membership, and event listing databases" },
];

export default function CfpScoutPage() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <main className="mx-auto max-w-[1200px] px-6 py-10">
        <div className="mb-6 space-y-3">
          <h1 className="text-lg font-medium">Conference Ecosystem Scout</h1>
          <p className="text-sm text-muted-foreground">
            Searches the web for CFP-driven events and the people, promoters,
            venues, and directories around them. Read-only — nothing here is
            saved to a database.
          </p>
          <div className="flex flex-wrap gap-2">
            {BUCKETS.map((bucket) => (
              <Badge key={bucket.label} variant="outline" title={bucket.description}>
                {bucket.label}
              </Badge>
            ))}
          </div>
        </div>
        <CfpScoutContent />
      </main>
    </div>
  );
}
