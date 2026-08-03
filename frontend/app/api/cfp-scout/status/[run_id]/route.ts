import { NextRequest, NextResponse } from "next/server";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ run_id: string }> },
) {
  const { run_id } = await params;
  const apiBaseUrl = process.env.API_BASE_URL;

  if (!apiBaseUrl) {
    return NextResponse.json(
      { error: "API_BASE_URL is not defined" },
      { status: 500 },
    );
  }

  try {
    const response = await fetch(
      `${apiBaseUrl}/api/cfp-scout/status/${run_id}`,
      {
        headers: {
          "X-API-Key": process.env.API_KEY ?? "",
        },
        cache: "no-store",
      },
    );

    const data = await response.json();
    if (!response.ok) {
      return NextResponse.json(
        { error: data?.detail || "Failed to fetch CFP scout status" },
        { status: response.status },
      );
    }

    return NextResponse.json(data);
  } catch (error) {
    console.error("Error fetching CFP scout status:", error);
    return NextResponse.json(
      { error: "Failed to fetch CFP scout status" },
      { status: 500 },
    );
  }
}
