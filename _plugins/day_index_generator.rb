# frozen_string_literal: true

# DayIndexGenerator
# Creates a per-day landing page at /news/:year/:month/:day/ for every day
# that has at least one edition post. Each page lists the day's editions
# (Morning, Afternoon, Evening) with a one-line Big Picture tease extracted
# from the post content.

module DayIndex
  class Generator < Jekyll::Generator
    safe true
    priority :low  # Run after other generators so posts are fully processed

    EDITION_ORDER = {
      "morning"   => 0,
      "afternoon" => 1,
      "evening"   => 2,
    }.freeze

    def generate(site)
      # Group posts by date (YYYY-MM-DD)
      posts_by_date = {}
      site.posts.docs.each do |post|
        next unless post.data["layout"] == "post"
        date_key = post.date.strftime("%Y-%m-%d")
        posts_by_date[date_key] ||= []
        posts_by_date[date_key] << post
      end

      posts_by_date.each do |date_key, posts|
        # Sort editions: Morning < Afternoon < Evening
        posts.sort_by! do |p|
          title = (p.data["title"] || "").downcase
          EDITION_ORDER.find { |k, _| title.include?(k) }&.last || 99
        end

        # Build edition data with Big Picture teasers
        editions = posts.map do |post|
          bp_tease = extract_big_picture_tease(post.content || "")
          {
            "title"    => post.data["title"],
            "url"      => post.url,
            "date"     => post.date,
            "bp_tease" => bp_tease,
          }
        end

        year, month, day = date_key.split("-")
        dir = "/news/#{year}/#{month}/#{day}"

        page = DayIndexPage.new(site, site.source, dir, "index.html", date_key, editions)
        site.pages << page
      end
    end

    private

    # Extract the first sentence of the Big Picture paragraph from post HTML
    def extract_big_picture_tease(content)
      # Match the Big Picture section: heading, optional audio div, then <p>
      if content =~ %r{<h3[^>]*>🌍\s*The Big Picture\s*</h3>.*?<p>(.*?)</p>}mi
        bp_text = $1
        # Strip HTML tags
        bp_tease = bp_text.gsub(%r{</?[^>]+>}, "").strip
        # Strip Markdown bold markers
        bp_tease = bp_tease.gsub(/\*\*/, "").strip
        # Remove leading "The Big Picture" label if present (legacy)
        bp_tease = bp_tease.sub(/\AThe Big Picture\s*/i, "").strip
        # Truncate to first sentence, max 200 chars
        first_sentence = bp_tease.split(/(?<=[.!?])\s/).first || bp_tease
        first_sentence.length > 200 ? first_sentence[0, 197] + "..." : first_sentence
      else
        ""
      end
    end
  end

  class DayIndexPage < Jekyll::Page
    def initialize(site, base, dir, name, date_key, editions)
      @site  = site
      @base  = base
      @dir   = dir
      @name  = name

      @path = File.join(base, dir.sub(%r{^/}, ""), name)

      process(name)

      self.data = {
        "layout"   => "day",
        "title"    => "AI News Digest — #{format_date(date_key)}",
        "date_key" => date_key,
        "editions" => editions,
      }

      self.content = ""
    end

    private

    def format_date(date_key)
      year, month, day = date_key.split("-").map(&:to_i)
      months = %w[January February March April May June July August September October November December]
      "#{months[month - 1]} #{day}, #{year}"
    end
  end
end