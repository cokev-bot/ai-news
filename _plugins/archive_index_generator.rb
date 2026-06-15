# frozen_string_literal: true

# ArchiveIndexGenerator
# Creates an /archive/ page that groups all edition posts by month (YYYY-MM)
# with per-month edition counts. Gives readers and crawlers a navigable
# structure beyond a flat list of posts.

module ArchiveIndex
  class Generator < Jekyll::Generator
    safe true
    priority :low

    def generate(site)
      # Group posts by month (YYYY-MM)
      posts_by_month = {}
      site.posts.docs.each do |post|
        next unless post.data["layout"] == "post"
        month_key = post.date.strftime("%Y-%m")
        posts_by_month[month_key] ||= []
        posts_by_month[month_key] << post
      end

      # Sort months in reverse chronological order
      sorted_months = posts_by_month.keys.sort.reverse

      # Build month data with counts
      months = sorted_months.map do |month_key|
        posts = posts_by_month[month_key]
        # Sort posts within each month by date ascending
        posts.sort_by!(&:date)

        edition_entries = posts.map do |post|
          {
            "title" => post.data["title"],
            "url"   => post.url,
            "date"  => post.date,
          }
        end

        {
          "month_key"    => month_key,
          "month_label"  => format_month(month_key),
          "count"        => posts.length,
          "editions"     => edition_entries,
        }
      end

      # Create a single archive page at /archive/
      dir = "/archive"
      page = ArchiveIndexPage.new(site, site.source, dir, "index.html", months)
      site.pages << page
    end

    private

    def format_month(month_key)
      year, month = month_key.split("-").map(&:to_i)
      months = %w[January February March April May June July August September October November December]
      "#{months[month - 1]} #{year}"
    end
  end

  class ArchiveIndexPage < Jekyll::Page
    def initialize(site, base, dir, name, months)
      @site  = site
      @base  = base
      @dir   = dir
      @name  = name

      @path = File.join(base, dir.sub(%r{^/}, ""), name)

      process(name)

      self.data = {
        "layout" => "archive",
        "title"  => "Archive",
        "months" => months,
      }

      self.content = ""
    end
  end
end