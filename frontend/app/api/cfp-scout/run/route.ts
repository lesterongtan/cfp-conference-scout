import { NextRequest, NextResponse } from "next/server";

export async function POST(request: NextRequest) {
  const apiBaseUrl = process.env.API_BASE_URL;

  if (!apiBaseUrl) {
    return NextResponse.json(
      { error: "API_BASE_URL is not defined" },
      { status: 500 },
    );
  }

  try {
    const body = await request.json();

    const response = await fetch(`${apiBaseUrl}/api/cfp-scout/run`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": process.env.API_KEY ?? "",
      },
      body: JSON.stringify(body),
    });

    const data = await response.json();
    if (!response.ok) {
      return NextResponse.json(
        { error: data?.detail || "Failed to start CFP scout" },
        { status: response.status },
      );
    }

    return NextResponse.json(data);
  } catch (error) {
    console.error("Error starting CFP scout:", error);
    return NextResponse.json(
      { error: "Failed to start CFP scout" },
      { status: 500 },
    );
  }
}
