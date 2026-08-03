import { CfpScoutContent } from "./cfp-scout-content";

export default function CfpScoutPage() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <main className="mx-auto max-w-[1200px] px-6 py-10">
        <div className="mb-6">
          <h1 className="text-lg font-medium">CFP / Conference Scout</h1>
          <p className="text-sm text-muted-foreground">
            Searches the web for conferences and events with an open call for
            speakers. Read-only — nothing here is saved to a database.
          </p>
        </div>
        <CfpScoutContent />
      </main>
    </div>
  );
}
